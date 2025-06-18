# server.py
import logging
from fastapi import FastAPI
import asyncio
import websockets
from pydantic import BaseModel
from uuid import uuid4
from datetime import datetime
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
import json
import collekto_data_fetcher.collekto_data_fetcher as data_fetcher
import os
import pandas as pd
from realtime_client import RealtimeClient
import base64
import math

# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('server.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

app = FastAPI()

# Allow frontend connections
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

USERNAME = os.getenv('COLLEKTO_USERNAME')
PASSWORD= os.getenv('COLLEKTO_PASSWORD')
MOCK_MODE = os.getenv('MOCK_COLLEKTO_API')
 
# In-memory session store
session_store = {}

# Request model
class CallInitiateModel(BaseModel):
    customer_name: str
    system_id: str  #68240b1240ee32c9049c41b7
    loan_id: str #C02504204479230106
    due_date: str
    due_amount: float
    product: str
    # customer_rating: str

# Response model
class CallSessionResponseModel(BaseModel):
    call_id: str
    created_at: str
    status: str
    customer_name: str
    system_id: str #68240b1240ee32c9049c41b7
    loan_id: str #C02504204479230106
    due_date: str
    due_amount: float
    product: str
    # customer_rating: str
    initiate: CallInitiateModel


def clean_nans(d):
    """Clean NaN values from dictionary, replacing them with None"""
    return {
        k: (None if isinstance(v, float) and math.isnan(v) else v)
        for k, v in d.items()
    }


@app.post("/start-call", response_model=CallSessionResponseModel)
async def start_call(data: CallInitiateModel):
    # Simulate generation of UUIDs and timestamps
    now = datetime.utcnow().isoformat() + "Z"
    call_id = str(uuid4())

    # Store initial session data
    session_store[call_id] = {
        "customer": {
            "name": data.customer_name,
            "system_id": data.system_id,
            "loan_id": data.loan_id,
            "due_date": data.due_date,
            "due_amount": data.due_amount,
            "product": data.product,
            # "rating": data.customer_rating
        },
        "call_metadata" : {},
        "disposition": {},
    }

    # Simulate fetching more customer info
    print(f'MOCK_MODE - {MOCK_MODE}')
    if MOCK_MODE:
        more_info = fetch_additional_customer_info(data.loan_id, data.system_id)
    else:
        more_info = data_fetcher.run_ltfs_flow(USERNAME, PASSWORD, data.loan_id, data.system_id)
    # cleaned_data = clean_nans(more_info)
    cleaned_data = more_info
    print(f"Cleaned more info: {cleaned_data}")

    
    print(session_store[call_id])
    session_store[call_id]['call_metadata'] = cleaned_data
    
    asyncio.create_task(connect_websocket(call_id))

    response = CallSessionResponseModel(
        call_id=call_id,
        customer_name= data.customer_name,
        system_id=data.system_id,
        loan_id= data.loan_id,
        due_date = data.due_date,
        due_amount= data.due_amount,
        product = data.product,
        # customer_rating= data.customer_rating,
        created_at=now,
        status="COMPLETED",
        initiate=data
    )
    return response


def fetch_additional_customer_info(loan_id: str, system_id: str) -> dict:
    """
    Simulate an external API call to fetch more customer details
    """
    try:
        # Load JSON file
        df = pd.read_csv('data.csv')

        matched_rows = df[(df['Loan_ID'] == loan_id) & (df['system_id'] == system_id)]
        print(f'Checking data.csv of size {len(df)} for loan_id = {loan_id} and system_id = {system_id}')

        if not matched_rows.empty:
            return matched_rows.iloc[0].to_dict()
        else:
            print("No matching customer found.")
            return {}
    except Exception as e:
        print(f"Error fetching additional customer info: {e}")
        return {}

async def connect_websocket(call_id: str):
    await asyncio.sleep(1.0)
    uri = f"ws://0.0.0.0:9000/wss/{call_id}"
    await asyncio.sleep(0.2)  
    print(f"Attempting to connect to WebSocket: {uri}")
    try:
        async with websockets.connect(uri) as websocket:
            print(f"Connected to websocket: {uri}")
            while True:
                try:
                    msg = await websocket.recv()
                    print(f"Received from WS server: {msg}")
                except websockets.ConnectionClosed:
                    print("WebSocket connection closed by server.")
                    break
    except Exception as e:
        print(f"[Self-Connect] WebSocket connection error: {e}")


@app.websocket("/wss/{call_id}")
async def websocket_endpoint(websocket: WebSocket, call_id: str):
    logger.info(f"[WS-{call_id}] New connection request")
    await websocket.accept()
    print(f"[WS-{call_id}] Connection accepted.")
    logger.info(f"[WS-{call_id}] Connection accepted")

    async def send_text(text: str):
        try:
            await websocket.send_json({"kind": "Text", "text": text})
            logger.debug(f"[WS-{call_id}] Sent text: {text[:50]}...")
        except Exception as e:
            logger.error(f"[WS-{call_id}] Error sending text: {e}")

    async def send_audio(audio_bytes: bytes):
        try:
            encoded = base64.b64encode(audio_bytes).decode()
            await websocket.send_json({"kind": "AudioData", "data": encoded})
            logger.debug(f"[WS-{call_id}] Sent audio chunk size: {len(audio_bytes)}")
        except Exception as e:
            logger.error(f"[WS-{call_id}] Error sending audio: {e}")

    client = RealtimeClient(
        response_handler=send_text,
        audio_handler=send_audio, 
        session_data = session_store[call_id]['call_metadata'])
    await client.connect()
    logger.info(f"[WS-{call_id}] RealtimeClient connected")

    try:
        while True:
            msg = await websocket.receive_text()
            
            payload = json.loads(msg)
            kind = payload.get("kind")
            logger.debug(f"[WS-{call_id}] Received message kind: {kind}")
            

            if kind == "AudioData":
                audio_bytes = base64.b64decode(payload["data"])
                await client.append_input_audio(audio_bytes)
            elif kind == "StopAudio":
                await client.commit_audio()
                await client.create_response()

            # Check if conversation is done
            # logger.info(f'checking conversation done status {client.conversation_done}, {client.is_conversation_done()}')
            if client.is_conversation_done():
                logger.info(f"[WS-{call_id}] Conversation done, sending stop signal")
                await websocket.send_json({"kind": "StopAudio"})
                await websocket.close()
                print(f"[WS-{call_id}] Conversation ended and socket closed.")
                break

    except WebSocketDisconnect:
        logger.warning(f"[WS-{call_id}] Client disconnected")
    except Exception as e:
        logger.error(f"[WS-{call_id}] Unexpected error: {e}", exc_info=True)
    finally:
        if client:
            try:
                # Clean up client resources if needed
                await client.disconnect()
            except Exception as e:
                logger.error(f"[WS-{call_id}] Error cleaning up client: {e}")