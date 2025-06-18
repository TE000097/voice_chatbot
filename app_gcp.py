import logging
import chainlit as cl
import os
import asyncio
import traceback
import requests
import websockets
import json
import pandas as pd
import base64

# Add logging configuration
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('client.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

os.environ["CHAINLIT_ENABLE_AUDIO"] = "true"
os.environ["CHAINLIT_DEBUG"] = "true"

url = 'collection-voicebot-932195943053.asia-south1.run.app'
# url = '0.0.0.0:9000'

# Store websocket client per user session
class WSClient:
    def __init__(self):
        self.ws = None
        self.recv_task = None
        self.connected = False
        self.closing = False

    async def connect(self, call_id):
        uri = f"wss://{url}/wss/{call_id}"  
        try:
            self.ws = await websockets.connect(uri)
            self.connected = True
            self.closing = False
            self.recv_task = asyncio.create_task(self.recv_loop())
            logger.info(f"Connected to websocket {uri}")
            return True
        except Exception as e:
            logger.error(f"Failed to connect websocket: {e}")
            return False

    async def recv_loop(self):
        '''
        Continuously listens for incoming messages from the backend. 
        '''
        try:
            async for message in self.ws:

                if self.closing:
                    break

                data = json.loads(message)
                kind = data.get("kind")
                
                if kind == "AudioData":
                    audio_b64 = data.get("data")
                    if audio_b64:
                        audio_bytes = base64.b64decode(audio_b64)
                        await cl.context.emitter.send_audio_chunk(
                            cl.OutputAudioChunk(
                                mimeType="audio/wav",
                                data=audio_bytes
                            ) # type: ignore
                        )

                elif kind == "Text":
                    text = data.get("data")
                    if text:
                      await cl.Message(content=f"Bot: {text}").send()
                elif kind == "StopAudio":
                    logger.info("Received stop signal from server")
                    await self.handle_stop()
                    break

        except websockets.ConnectionClosedOK:
            logger.info("WebSocket connection closed normally")
            if not self.closing:
                await self.handle_stop()
        except websockets.ConnectionClosedError as e:
            logger.error(f"WebSocket connection closed with error: {e}")
            if not self.closing:
                await self.handle_stop()
        except Exception as e:
            logger.error(f"Error in websocket receive loop: {e}", exc_info=True)
            if not self.closing:
                await self.handle_stop()
        finally:
            self.connected = False

    async def handle_stop(self):
        '''Handles the stop signal from the server.'''
        self.connected = False
        if not self.closing:
            self.closing = True
            await cl.Message(content="Connection closed by server. Session ended.").send()

    async def send_audio(self, audio_bytes):
        if not self.ws or not self.connected or self.closing:
            logger.warning("Cannot send audio - connection not active")
            return False
            
        try:
            audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")
            msg = json.dumps({"kind": "AudioData", "data": audio_b64})
            await self.ws.send(msg)
            return True
        except websockets.ConnectionClosed:
            logger.info("Connection closed while sending audio")
            await self.handle_stop()
            return False
        except Exception as e:
            logger.error(f"Error sending audio: {e}")
            return False

    async def stop(self):
        if self.ws and self.connected and not self.closing:
            try:
                self.closing = True
                await self.ws.send(json.dumps({"kind": "StopAudio"}))
                await self.ws.close()
            except websockets.ConnectionClosed:
                logger.info("Connection already closed")
            except Exception as e:
                logger.error(f"Error during stop: {e}")
            finally:
                self.connected = False
                if self.recv_task:
                    self.recv_task.cancel()
                    self.recv_task = None

def trigger_start_call():
    df = pd.read_csv("api_initiate.csv")
    selected = df.sample(1).iloc[0]

    payload = {
    "customer_name": "Harish",
    "system_id":"68240b1240ee32c9049c41b7",
    "loan_id": "C02504204479230106",
    "due_date": "2025-06-03 0:00:00",
    "due_amount":"5175",
    "product": "CL"
}

    try:
        response = requests.post(f"https://{url}/start-call", json=payload)
        logger.info(f"Fast API Response - {response.json()}")
        session_id = response.json().get("call_id")
        logger.info(f"Session ID: {session_id}")
        return session_id
    except Exception as e:
        logger.error(f"Failed to start call via REST: {e}")
        return None

@cl.on_audio_start
async def on_audio_start():
    try:
        await cl.Message(content="Starting new audio session...").send()

        loop = asyncio.get_event_loop()
        session_id = await loop.run_in_executor(None, trigger_start_call)
        cl.user_session.set("session_id", session_id)
        if not session_id:
            await cl.Message(content="Failed to get call session ID").send()
            return False

        ws_client = WSClient()
        connected = await ws_client.connect(session_id)
        if not connected:
            await cl.Message(content="Failed to connect to WebSocket server").send()
            return False

        cl.user_session.set("ws_client", ws_client)
        return True

    except Exception as e:
        await cl.Message(content=f"Failed to start audio session: {e}", type="system_message").send()
        traceback.print_exc()
        return False

@cl.on_audio_chunk
async def on_audio_chunk(audio_chunk: cl.InputAudioChunk):
    try:
        ws_client = cl.user_session.get("ws_client")
        if ws_client and ws_client.connected:
            await ws_client.send_audio(audio_chunk.data)
            return True
        else:
            await cl.Message(content="WebSocket not connected. Please start audio session first.").send()
            return False
    except Exception as e:
        print(f"Error in on_audio_chunk: {e}")
        traceback.print_exc()
        return False

@cl.on_audio_end
@cl.on_chat_end
@cl.on_stop
async def on_end():
    try:
        ws_client = cl.user_session.get("ws_client")
        if ws_client and ws_client.connected:
            await ws_client.stop()
            await cl.Message(content="Audio session ended.").send()
    except Exception as e:
        traceback.print_exc()
