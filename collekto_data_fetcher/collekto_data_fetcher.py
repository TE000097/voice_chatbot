"""
LTFS API Client Module

This module provides functions to authenticate to the LTFS API, fetch loan details,
and retrieve call disposition (case history). It uses AES encryption compatible
with the frontend's CryptoJS settings to encrypt passwords.
"""
import base64
import logging
from typing import Any, Dict
import os
import dotenv
import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from collekto_data_fetcher import constants

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

dotenv.load_dotenv()
# Constants
USERNAME: str = os.getenv("USERNAME") #"arushiagrawal@ltfs.com"
PASSWORD: str = os.getenv("PASSWORD") #"Arushi.ltfs@2025"
# LOAN_ID = "67e9d847dcdc29c021f9ae85"
# CASE_LOAN_ID = "JG00011223300"
BASE_URL: str = constants.BASE_URL #"https://backendcrmuat.ltfinance.com"
RAW_KEY: str = constants.RAW_KEY#"collektoencrypte"
AUTH_PATH: str = constants.AUTH_PATH #"/api/v2/profile/authenticate"
LOAN_PATH: str = constants.LOAN_PATH #"/crm/api/v1/loans/id"
DISP_PATH: str = constants.DISP_PATH #"/api/v1/call-disposition/caseHistory"


class AuthenticationError(Exception):
    """Exception raised when authentication fails."""


class APIError(Exception):
    """Exception raised when an API request fails."""


def _encrypt_password(raw_password: str) -> str:
    """
    Encrypt the password using AES-ECB with PKCS7 padding.

    Args:
        raw_password: The plaintext password.

    Returns:
        The Base64-encoded encrypted password.
    """
    # Base64-encode the raw key and decode back to bytes
    b64_key = base64.b64encode(RAW_KEY.encode("utf-8"))
    key_bytes = base64.b64decode(b64_key)

    cipher = AES.new(key_bytes, AES.MODE_ECB)
    padded = pad(raw_password.encode("utf-8"), AES.block_size, style="pkcs7")
    encrypted_bytes = cipher.encrypt(padded)

    return base64.b64encode(encrypted_bytes).decode("utf-8")


def authenticate(username: str, password: str) -> str:
    """
    Authenticate with the LTFS API and retrieve an access token.

    Args:
        username: The login username.
        password: The plaintext password.

    Returns:
        The authentication token string.

    Raises:
        AuthenticationError: If authentication fails or response is malformed.
    """
    encrypted_password = _encrypt_password(password)
    url = f"{BASE_URL}{AUTH_PATH}"
    payload: Dict[str, str] = {"username": username, "password": encrypted_password}
    headers: Dict[str, str] = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    response = requests.post(url, json=payload, headers=headers, timeout=10)
    if not response.ok:
        logger.error("Authentication failed.Status %d: %s", response.status_code, response.text)
        raise AuthenticationError("Failed to authenticate with LTFS API")

    try:
        data = response.json()
        token = data["data"]["authenticationResult"]["bdInfoGHKey_1000"]
    except (ValueError, KeyError) as error:
        logger.exception("Error parsing authentication response")
        raise AuthenticationError("Malformed authentication response") from error

    return token


def get_loan_by_id(token: str, loan_id: str) -> Dict[str, Any]:
    """
    Retrieve loan details by loan ID.

    Args:
        token: The access token.
        loan_id: The loan identifier.

    Returns:
        A dictionary with loan data.

    Raises:
        APIError: If the request fails.
    """
    url = f"{BASE_URL}{LOAN_PATH}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "id": loan_id,
    }

    response = requests.get(url, headers=headers, timeout=10)
    try:
        response.raise_for_status()
    except requests.HTTPError as error:
        logger.error("Error fetching loan %s: %s", loan_id, response.text)
        raise APIError(f"Failed to fetch loan {loan_id}") from error

    return response.json()


def get_disposition_by_id(token: str, loan_id: str) -> Dict[str, Any]:
    """
    Retrieve call disposition (case history) by loan ID.

    Args:
        token: The access token.
        loan_id: The loan identifier for case history.

    Returns:
        A dictionary with disposition data.

    Raises:
        APIError: If the request fails.
    """
    url = f"{BASE_URL}{DISP_PATH}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "loanid": loan_id,
    }

    response = requests.get(url, headers=headers, timeout=10)
    try:
        response.raise_for_status()
    except requests.HTTPError as error:
        logger.error("Error fetching disposition %s: %s", loan_id, response.text)
        raise APIError(f"Failed to fetch disposition for {loan_id}") from error

    return response.json()

def run_ltfs_flow(username: str, password: str, loan_id: str, case_loan_id: str):
    """
    Executes the LTFS flow: authenticate, fetch loan, fetch disposition.

    Args:
        username: LTFS API username.
        password: LTFS API password.
        loan_id: Loan ID for fetching loan details.
        case_loan_id: Case ID for fetching disposition history.
    """
    loan_data = []
    disposition_data = []

    try:
        logger.info("Authenticating...")
        token = authenticate(username, password)
        logger.info("Got token: %s", token)
        logger.info("##########\n")

        logger.info("Fetching loan %s...", loan_id)
        loan_data = get_loan_by_id(token, loan_id)
        logger.info("Loan response: %s", loan_data)
        logger.info("##########\n")

        logger.info("Fetching case history for %s...", case_loan_id)
        disposition_data = get_disposition_by_id(token, case_loan_id)
        logger.info("Disposition response: %s", disposition_data)
        logger.info("##########\n")


    except (AuthenticationError, APIError) as err1:
        logger.error("Operation failed: %s. \n Using default credentials...\n", err1)

        try:
            logger.info("Authenticating with default credentials...")
            token = authenticate(USERNAME, PASSWORD)
            logger.info("Got token: %s", token)
            logger.info("##########\n")

            logger.info("Fetching loan %s...", LOAN_ID)
            loan_data = get_loan_by_id(token, LOAN_ID)
            logger.info("Loan response: %s", loan_data)
            logger.info("##########\n")

            logger.info("Fetching case history for %s...", CASE_LOAN_ID)
            disposition_data = get_disposition_by_id(token, CASE_LOAN_ID)
            logger.info("Disposition response: %s", disposition_data)
            logger.info("##########\n")

        except (AuthenticationError, APIError) as err2:
            logger.error("Operation failed: %s", err2)

    return loan_data, disposition_data


# if __name__ == "__main__":
    # run_ltfs_flow(USERNAME, PASSWORD, LOAN_ID, CASE_LOAN_ID)