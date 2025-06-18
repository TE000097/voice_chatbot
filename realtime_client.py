# --- realtime_client.py ---

import logging
import os
import traceback
import asyncio
import base64
from openai import AsyncAzureOpenAI
from dotenv import load_dotenv
import pandas as pd
import json
from openai.types.beta.realtime import ConversationItemCreateEvent
import tools

# Configure logger only once at module level
logger = logging.getLogger(__name__)
# Prevent duplicate handlers
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
logger.propagate = False  # Prevents logs from being passed to root logger

load_dotenv()
AZURE_OPENAI_API_KEY = os.getenv("AZURE_OPENAI_API_KEY")
AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT")
AZURE_OPENAI_DEPLOYMENT = "gpt-4o-realtime-preview"
AZURE_API_VERSION = "2025-04-01-preview"

class RealtimeClient:
    def __init__(self,
                 response_handler=None, 
                 audio_handler=None, 
                 interrupt_handler=None, 
                 on_text=None, 
                 on_audio=None, 
                 session_data = None):
        self.connection = None
        self.connection_manager = None
        self.connected = False
        self.audio_buffer = []
        self.transcript = ""
        self.response_handler = response_handler
        self.audio_handler = audio_handler
        self.interrupt_handler = interrupt_handler
        self.chat_history = [] 
        self._responding = False
        self.on_text = on_text
        self.on_audio = on_audio
        self.customer_data = session_data
        self.conversation_done = False

        print(f'In realtime client - {self.customer_data}')

        print(f'In realtime client get customer name - {self.customer_data["Debtor_Name"]}')

        self.config = {
            "voice": "alloy",
            "instructions": get_system_prompt(self.customer_data),
            "modalities": ["audio", "text"],
            "input_audio_format": "pcm16",
            "output_audio_format": "pcm16",
            "input_audio_transcription": {
                "model": "whisper-1", 
                },
            "turn_detection": {
                "type": "server_vad",
                "threshold": 0.6,
                "prefix_padding_ms": 1000,
                "silence_duration_ms": 2000,
                "create_response": True,
                "interrupt_response": True
            },
            "tools" : tools.tools_defined,
            "tool_choice": "auto"
        }


    def is_connected(self):
        return self.connected

    async def connect(self):
        try:
            if not AZURE_OPENAI_ENDPOINT or not AZURE_OPENAI_API_KEY or not AZURE_API_VERSION or not AZURE_OPENAI_DEPLOYMENT:
                raise ValueError("Missing one or more required environment variables")
            if self.connection_manager:
                await self.disconnect()

            client = AsyncAzureOpenAI(
                api_key=AZURE_OPENAI_API_KEY,
                azure_endpoint=AZURE_OPENAI_ENDPOINT,
                api_version=AZURE_API_VERSION
            )
            self.connection_manager = client.beta.realtime.connect(model=AZURE_OPENAI_DEPLOYMENT)
            self.connection = await self.connection_manager.__aenter__()
            await self.connection.session.update(session=self.config)
            self.connected = True

            asyncio.create_task(self.handle_response(self.connection))
            print('Realtime connected')
            return True

        except Exception as e:
            self.connected = False
            logger.error(f"[RealtimeClient] Failed to connect: {e}")
            logger.error(traceback.print_exc())
            raise

    async def disconnect(self):
        if self.connection_manager:
            try:
                await self.connection_manager.__aexit__(None, None, None)
            except Exception as e:
                logger.error(f"[RealtimeClient] Error during disconnect: {e}")
        self.connected = False

    async def append_input_audio(self, chunk):
        if self.connection:
            await self.connection.send({
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(chunk).decode("utf-8")
            }) 

    async def commit_audio(self):
        if self.connection:
            await self.connection.send({"type": "input_audio_buffer.commit"})

    async def create_response(self):
        if self.connection and not self._responding:
            try:
                self._responding = True
                await self.connection.send({"type": "response.create"})
            except Exception as e:
                logger.error(f"Error creating response: {e}")
                self._responding = False

    async def send_user_message_content(self, content=[]):
        if content and self.connection:
            await self.connection.send({"type": "conversation.item.create",                                  
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": content,
                }
            })
        await self.create_response()
        return True

    async def handle_response(self, connection):
        pending_tool_calls ={}
        try:
            async for event in connection:
                logger.info(f'Event {event.type}')

                # Handle error events first
                if event.type == "error":
                    logger.error(f"[RealtimeClient] Error event: {event}")
                    logger.error(traceback.format_exc())
                    continue

                elif event.type == "conversation.item.input_audio_transcription.completed":
                    user_transcript = event.transcript.strip()
                    if user_transcript:
                        user_message = {
                            "role": "user",
                            "content": user_transcript
                        }
                        self.chat_history.append(user_message)
                        logger.info(f"User said: {user_message}")
                        await asyncio.sleep(0.1)
                        await self.create_response()

                elif event.type == "response.audio.delta" and self.audio_handler:
                        audio_bytes = base64.b64decode(event.delta)
                        await self.audio_handler(audio_bytes)

                elif event.type in {"response.text.delta", "response.audio_transcript.delta"}:
                        self.transcript += event.delta

                elif event.type == "response.done":
                   if self.transcript.strip():
                        assistant_message = {
                            "role": "assistant",
                            "content": self.transcript.strip()
                        }
                        self.chat_history.append(assistant_message)
                        logger.info(f"Added assistant message to history: {assistant_message}")
                        
                        if self.response_handler:
                            await self.response_handler(self.transcript.strip())

                        self.conversation_done = "[END_CONVERSATION]" in assistant_message["content"]
                        self.transcript = ""
                        self.audio_buffer.clear()
                        self._responding = False


                elif event.type == "response.output_item.added" and getattr(event.item, "type", None) == "function_call":
                    cid = event.item.call_id
                    pending_tool_calls[cid] = {
                        "name": event.item.name,
                        "call_id": cid,
                        "arguments": {}
                    }

                # Full args for the tool are in
                elif event.type == "response.function_call_arguments.done":
                    cid = event.call_id
                    if cid in pending_tool_calls:
                        # parse args
                        args = self.customer_data #json.loads(event.arguments or "{}")
                        pending_tool_calls[cid]["arguments"] = args
                        call = pending_tool_calls[cid]
                        call["arguments"] = args
                        logger.info(f"Invoking tool `{call['name']}` with {args}")

                        # dispatch
                        if call["name"] == "check_payment_status":
                            result = tools.check_payment_status(
                                DPD=(args.get("DPD", 0) if isinstance(args, dict) else 0)
                            )
                        else:
                            result = "Unknown tool"
                        
                        logger.info(f"Tool `{call['name']}` returned: {result} for DPD {args.get('DPD', 0)}")

                        # send back function_call_output
                        await connection.conversation.item.create(
                            item={
                                "type": "function_call_output",
                                "call_id": cid,
                                "output": json.dumps(result)
                            }
                        )
                        # ask the model to continue
                        await connection.response.create()
                        del pending_tool_calls[cid]


        except Exception as e:
            logger.error(f"Response handling error: {str(e)}")
            logger.error(traceback.print_exc())
            return False

    async def get_chat_history(self):
        return self.chat_history

    def is_conversation_done(self):
        return self.conversation_done



def get_system_prompt(customer):

    name = customer['Debtor_Name']
    gender = customer['Gender']
    emi = customer['EMI_Amount']
    due_date = customer['Payment_Due_Date']
    loan_type = customer['Product']

    logger.info(f"{name}, {gender}, {emi}, {due_date}, {loan_type}")

    # Format amounts and dates for better readability
    formatted_emi = "{:,.2f}".format(float(emi))
    formatted_date = due_date  # Assuming due_date is already in proper format

    # Gender-specific honorific
    salutation = "Mr." if gender.lower() == "male" else "Ms."

    system_prompt = f"""

    System Prompt (Bot Instructions)
    You are a professional female voice bot calling on behalf of L&T Finance for Bucket - X Customers. 
    Your primary goal is to remind customers about their upcoming EMI payments, encourage timely payments, 
    and handle customer queries or objections effectively. Ensure a polite, professional, 
    and empathetic tone while following the structured conversation flow. Adapt to different customer 
    responses and provide clear payment instructions. 

    The customer is a Hindi speaker, so use Hindi for the conversation. 
    Use simple Hindi and commonly spoken English words for better customer engagement.
    After each sttatement, wait for the customer to respond.

    Your key objectives are:
        Verify the customer’s identity.
        Mention that payment is delayed and understand the reason for payment delay.
        Provide alternate solutions for payment.
        Explain the consequences of non-payment.
        Encourage the customer to make the payment.
        Provide payment options and assist with the payment process.
        Maintain a polite and professional tone throughout the conversation.
        
    You have the following tools at your disposal - check_payment_status. Use it when the customer says that they have already done the payment.
    When customer says that the payment is already completed, then invoke check_payment_status tool. 
    If the tool returns "Payment completed" as output, then agree that the customer is corrent, and proceed towards ending the call.
    If the tool returns "Payment not completed", then politely inform the customer that the payment is not completed and proceed with the conversation.

        CUSTOMER DETAILS (USE THESE IN CONVERSATION):
        - Customer Name: {name}
        - Gender: {gender}
        - Salutation: {salutation}
        - EMI Amount Due: ₹{formatted_emi}
        - Loan Type: {loan_type}

         IMPORTANT: Always use these specific details in your conversation:
         - Always address the customer as {salutation} {name}
         - Mention the exact EMI amount: ₹{formatted_emi}
         - Reference the specific due date: {formatted_date}

     2. Start your conversation with Customer Verification
         Kya meri baat {salutation} {name} se ho rahi hai? Wait for customer to respond. 
         If Nahi: Kya main jaan sakti hoon ki aapka {salutation} {name} se kya sambandh hai?
         Ask if they are aware of the loan:
             If yes: Kya aap unke 2-Wheeler loan ke baare mein jaante hai?
             If the person is aware of the loan: Proceed with the call.
             If the person is unaware: Kya mujhe customer ka koi alternate contact mil sakta hai? Aur unhe call karne ka acha samay kya hoga?
         If YES: Proceed with the call

     3. Purpose of Call
         Purpose: Yeh call aapke L&T Finance ke two-wheeler loan ki EMI payment ke sambandh mein hai.

         1. EMI Reminder & Reason for Delay
         Aapki teen hazzar panch so rupee ki EMI due hai jo abhi tak pay nahi hui hai. 
         Kya aap bata sakte hain ki payment mein deri ka kya karan hai?

         2. If customer tells the reason for delay: 
             a. Empathise on the reason. 
             b. If it's medical related, ask if this is the right time to talk. If not, ask for alternate time and end this call. 
             c. If you proceed with the call - 
                 Mention the due date and charges
                 a. Due Date: - aapki EMI 3rd ko due thi, aaj already 5 tareek ho chuki hai.
                 b. Bounce Charges: - Rs. 500/- Mention on every call. 
                 c. Penalty Charges: - 2% late penalty charges on the EMI amount on a pro-rata basis.

         3. If the customer doesn't give proper reasoning – Further Probing:
             a. Kya main jaan sakta hoon ki aap salaried hain ya business chalate hain?
             b. Aap kis din apni current month ki EMI pay karne ka plan kar rahe hain?
             c. Jo date aapne batayi hai, us din aap funds kaise manage karenge, kya aap thoda sa idea de sakte hain?


         4. If customer rejects to make the payment: 
             Explain the consequences – If payment is not done on time, credit record will get affected. You may face challenges in acquiring new loans.
             Say something like:
             a. Aapke account pr already 3 baar bounce ke charges lge hue hai. Ye aapka penalty amount badta hi jaa rha hai.  
             b. Payment na krne pr aapka cibil score kharab ho jaega, jo aapke liye naye loans ya credit cards lene mein mushkil kar sakta hai.
             c. Agr aage chal kr kuch problem aayi, and aapko loan lene ki jroort hui. Pr cibil score kharab hone ki wajah se aapko naye loan lene mein dikkat aa sakti hai.

             Provide alternate solutions:
                 a. Kya aap apne kisi rishtedaar ya dost se temporary support le sakte hain?
                 b. Kya aap apni savings jaise Fixed Deposit ya Recurring Deposit se fund arrange kar sakte hain?
                 c. Agar aap salaried hain toh advance salary ka option explore kiya ja sakta hai; agar aap self-employed hain toh colleagues ya business savings se support mil sakta hai.
                 d. Aapke kisi investment jaise Shares, Mutual Funds, ya Debentures se bhi fund arrange karne ka vikalp ho sakta hai.


         5. If customer agrees to make the payment:
         Thank you Sir, Toh aap payment kaise karna chaahenge? (Pitch Digital mode first - "Kya Aap online payment kr skte hai abhi?") 
             1. If customer agrees for online payment-
                 Priority 1: Planet App - 
                 "Kya aapke pass LTFS Planet App hai?" Wait for the customer to respond
                 Ask if the customer has downloaded the PLANET App. Wait for the customer to respond. 
                     If yes: Thank the customer.
                     If no: Pitch the app: Ask them to download "LTFS – PLANET App" from the Play Store/App Store using their smartphone.
                 Mention “Quick Pay” Option: (Don't mention everything at once, mention it step by step and wait for the customer to respond)
                     Guide the customer to click on the “Quick Pay” option in the app.
                     Inform them that even an unregistered mobile number can be used to log in/download.
                     Explain Payment Options: Once on “Quick Pay,” the customer will see options like: Debit Card / Net Banking / Wallets / UPI
                     Reassure on Payment Confirmation: Inform them that payments made through the app will be updated in LTFS records within 30 minutes.
    
                 Example:
                 Sir, kya aapne PLANET App download kiya hua hai?
                 Agar haan:
                 Shukriya! ab aap uspr 'Quick Pay' option par click karein. Waha aapko kai payment options milenge jaise Debit Card, Net Banking, Wallets ya UPI. Aap ko jo best lg rha usse payment kr dijiye.
                 Payment karne ke baad woh 30 minutes ke andar LTFS ke records mein reflect ho jaayega.
                 Kya aapka payment hogya? 

                 Agar nahi:
                 Kripya apne smartphone mein “LTFS - PLANET App” download karein Play Store ya App Store se. Wait for customer to respond and download. 
                 And ask again if he has downloaded the app? Once done, proceed as before. 
    

             2. Priority 2 – Alternate Payment Modes - Pitch for Payment Link / BBPS / Website / NEFT / RTGS / Paytm
                 a. If customer is not able to download the app, ask them to use the payment link. Go below steps by steps, waiting for customer to respond. 
                     1. Share the payment link via SMS or WhatsApp.
                     2. Ask the customer to click the link. It will open multiple payment options: Debit Card, Net Banking, UPI, Wallet
                     3. Also suggest visiting the L&T Financial Services website and using the Quick Pay feature.
                     4. For NEFT/RTGS, share bank details (if applicable).
                     5. Request the customer to share the transaction ID once payment is done (for internal reference only).

                 Example:
                 Sir, aapke number par ek payment link bheja jaa raha hai.
                 Kripya us link par click karein, wahan aapko kai options milenge jaise Debit Card, Net Banking, UPI ya Wallets.
                 Aap www.ltfs.com par jaakar bhi “Quick Pay” ka use karke payment kar sakte hain.
                 Payment ke baad, kripya transaction ID share kar dein for record purpose.

             3. If Customer Does not agree for online payment: - Convince the customer and inform the benefits of online payment. If customer still does not agree.
                 Pitch for PRO payment options i.e., Airtel Payments bank / FINO / Pay World / PayU / Pay nearby & ITZ
                 Example : 
                 Sir, online payment se aapka time bachega aur payment turant confirm ho jaata hai.
                 Agar aap chahein toh aap nearby PRO payment centres jaise Airtel Payments Bank, FINO, Pay World, ya PayNearby ka use kar sakte hain. Wahaan se bhi aap asaani se payment kar sakte hain.

             4. If customer wants to visit the branch: 
                 Ask which branch the customer wants to visit.
                 Confirm the branch location and share the correct address if needed.
                 Example:
                 Sir, agar aap branch visit karna chahte hain toh kripya mujhe batayein kaunsi branch mein jaana chaahenge?
                 Main aapko us branch ka sahi location confirm kar deta hoon.


         8. If not ready to pay on the same call, Record the PTP date.
         Sir, please aap diye gaye date par payment krne ki koshish karein.

         9. Take additional details from customer before closing the call.
             1. Confirm the Vehicle User Status - ask the customer to confirm who is currently using the vehicle.
             Sir, kripya batayein ki gaadi ka istemal kaun kar raha hai?

             2. Confirm if there is any alternate contact number available.
             Kya aapka koi aur contact number hai jo aap file mein add krna chahte hai?

         10. Before ending the conversation, reply with appropriate closing statements.

         11. Wait for user's reply. 
         
         12. If the user doesn't ask anything or replies with closing argument (like - okay, thank you, theek hai etc.), end the conversation politely. End the whole conversation with the phrase [END_CONVERSATION]
    """
    
    logger.info(f"Generated prompt for customer: {name}")
    return system_prompt

logger.info("Logger initialized")
logger.info("################")
