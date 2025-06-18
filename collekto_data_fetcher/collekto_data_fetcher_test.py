import base64
import pytest
import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

import collekto_data_fetcher
from collekto_data_fetcher import (
    _encrypt_password,
    authenticate,
    get_loan_by_id,
    get_disposition_by_id,
    run_ltfs_flow,
    AuthenticationError,
    APIError,
    USERNAME,
    PASSWORD,
    LOAN_ID,
    CASE_LOAN_ID,
    RAW_KEY,
)


class DummyResponse:
    """
    Helper class to simulate `requests.Response` for testing.
    """
    def __init__(self, ok=True, status_code=200, json_data=None, text=""):
        self.ok = ok
        self.status_code = status_code
        self._json_data = json_data or {}
        self.text = text or ""

    def json(self):
        return self._json_data

    def raise_for_status(self):
        if not (200 <= self.status_code < 300):
            raise requests.HTTPError(f"Status {self.status_code}: {self.text}")


# -----------------------------
# Tests for _encrypt_password
# -----------------------------

def test_encrypt_password_consistency_and_uniqueness():
    # Two calls with the same raw password must produce identical ciphertext
    pw = "MySecretPassword123!"
    first_cipher = _encrypt_password(pw)
    second_cipher = _encrypt_password(pw)
    assert isinstance(first_cipher, str)
    assert first_cipher == second_cipher

    # A different password should produce a different ciphertext
    different_cipher = _encrypt_password(pw + "A")
    assert different_cipher != first_cipher

    # Check that the ciphertext is valid Base64 and decryptable back to the padded plaintext
    # We'll manually decrypt to verify it uses RAW_KEY correctly
    cipher_bytes = base64.b64decode(first_cipher)
    # RAW_KEY is ASCII; base64.b64encode -> decode, yields original bytes
    key_bytes = base64.b64decode(base64.b64encode(RAW_KEY.encode("utf-8")))
    cipher = AES.new(key_bytes, AES.MODE_ECB)
    decrypted_padded = cipher.decrypt(cipher_bytes)
    # Strip PKCS7 padding
    pad_len = decrypted_padded[-1]
    decrypted = decrypted_padded[:-pad_len].decode("utf-8")
    assert decrypted == pw


# -----------------------------
# Tests for authenticate()
# -----------------------------

def test_authenticate_success(monkeypatch):
    """
    Simulate a successful authentication: status_code=200, JSON returns the deep key.
    """
    dummy_token = "fake‐token‐123"

    def fake_post(url, json, headers, timeout):
        # Ensure the payload is correct: password should be encrypted
        encrypted = _encrypt_password("plaintext")
        assert json["password"] == encrypted
        return DummyResponse(
            ok=True,
            status_code=200,
            json_data={
                "data": {
                    "authenticationResult": {
                        "bdInfoGHKey_1000": dummy_token
                    }
                }
            },
        )

    monkeypatch.setattr(requests, "post", fake_post)
    token = authenticate("user@example.com", "plaintext")
    assert token == dummy_token


def test_authenticate_http_error(monkeypatch):
    """
    Simulate an HTTP‐error response (ok=False). Should raise AuthenticationError.
    """
    def fake_post(url, json, headers, timeout):
        return DummyResponse(ok=False, status_code=401, text="Unauthorized")

    monkeypatch.setattr(requests, "post", fake_post)

    with pytest.raises(AuthenticationError) as excinfo:
        authenticate("user@nope", "wrongpassword")
    assert "Failed to authenticate" in str(excinfo.value)


def test_authenticate_malformed_json(monkeypatch):
    """
    Simulate ok=True but missing the expected JSON structure. Should raise AuthenticationError.
    """
    # Case 1: response.json() raises ValueError
    class BrokenJSONResponse(DummyResponse):
        def json(self):
            raise ValueError("Invalid JSON")

    def fake_post1(url, json, headers, timeout):
        return BrokenJSONResponse(ok=True, status_code=200, text="{}")

    monkeypatch.setattr(requests, "post", fake_post1)
    with pytest.raises(AuthenticationError):
        authenticate("user@example.com", "any")

    # Case 2: JSON exists but missing nested keys
    def fake_post2(url, json, headers, timeout):
        return DummyResponse(ok=True, status_code=200, json_data={"wrong": {}})

    monkeypatch.setattr(requests, "post", fake_post2)
    with pytest.raises(AuthenticationError):
        authenticate("user@example.com", "any")


# ---------------------------------
# Tests for get_loan_by_id()
# ---------------------------------

def test_get_loan_by_id_success(monkeypatch):
    """
    Simulate a successful GET: status_code=200, returns some JSON.
    """
    dummy_loan = {"loanId": LOAN_ID, "amount": 25000}

    def fake_get(url, headers, timeout):
        assert headers["Authorization"].startswith("Bearer ")
        assert headers["id"] == LOAN_ID
        return DummyResponse(ok=True, status_code=200, json_data=dummy_loan)

    monkeypatch.setattr(requests, "get", fake_get)
    token = "dummy"
    result = get_loan_by_id(token, LOAN_ID)
    assert result == dummy_loan


def test_get_loan_by_id_http_error(monkeypatch):
    """
    Simulate a GET that returns 404 or other error => raise APIError.
    """
    def fake_get(url, headers, timeout):
        return DummyResponse(ok=False, status_code=404, text="Not Found")

    monkeypatch.setattr(requests, "get", fake_get)
    with pytest.raises(APIError) as excinfo:
        get_loan_by_id("dummy", "nonexistent")
    assert "Failed to fetch loan" in str(excinfo.value)


# ---------------------------------
# Tests for get_disposition_by_id()
# ---------------------------------

def test_get_disposition_by_id_success(monkeypatch):
    """
    Simulate a successful GET for disposition.
    """
    dummy_disp = {"caseId": CASE_LOAN_ID, "status": "Closed"}

    def fake_get(url, headers, timeout):
        assert headers["Authorization"].startswith("Bearer ")
        assert headers["id"] == CASE_LOAN_ID
        return DummyResponse(ok=True, status_code=200, json_data=dummy_disp)

    monkeypatch.setattr(requests, "get", fake_get)
    token = "dummy"
    result = get_disposition_by_id(token, CASE_LOAN_ID)
    assert result == dummy_disp


def test_get_disposition_by_id_http_error(monkeypatch):
    """
    Simulate HTTP error on GET disposition => APIError.
    """
    def fake_get(url, headers, timeout):
        return DummyResponse(ok=False, status_code=500, text="Server Error")

    monkeypatch.setattr(requests, "get", fake_get)
    with pytest.raises(APIError) as excinfo:
        get_disposition_by_id("dummy", "wrong")
    assert "Failed to fetch disposition" in str(excinfo.value)


# ---------------------------------
# Tests for run_ltfs_flow()
# ---------------------------------

def test_run_ltfs_flow_all_success(monkeypatch):
    """
    Primary path: primary authenticate, get_loan_by_id, get_disposition_by_id all succeed.
    """
    # 1) Mock authenticate to return a token
    primary_token = "primary‐token"
    def fake_auth(username, password):
        assert username == "user1"
        assert password == "pass1"
        return primary_token

    # 2) Mock get_loan_by_id and get_disposition_by_id to return dummy data
    dummy_loan = {"loanId": "X"}
    dummy_disp = {"caseId": "Y"}

    monkeypatch.setattr(collekto_data_fetcher, "authenticate", fake_auth)
    monkeypatch.setattr(collekto_data_fetcher, "get_loan_by_id", lambda t, lid: dummy_loan)
    monkeypatch.setattr(collekto_data_fetcher, "get_disposition_by_id", lambda t, cid: dummy_disp)

    loan_data, disp_data = run_ltfs_flow("user1", "pass1", "LID", "CID")
    assert loan_data == dummy_loan
    assert disp_data == dummy_disp


def test_run_ltfs_flow_primary_fail_fallback_success(monkeypatch):
    """
    Simulate primary authenticate raising AuthenticationError,
    then fallback with USERNAME/PASSWORD succeeds.
    """
    # 1) First call to authenticate => AuthenticationError
    calls = {"count": 0}
    def fake_auth(username, password):
        calls["count"] += 1
        if calls["count"] == 1:
            raise AuthenticationError("Bad creds")
        # Second call should be fallback using default USERNAME/PASSWORD
        assert username == USERNAME
        assert password == PASSWORD
        return "fallback‐token"

    # 2) When fallback, get_loan_by_id and get_disposition_by_id return known data
    fallback_loan = {"loanId": LOAN_ID}
    fallback_disp = {"caseId": CASE_LOAN_ID}

    monkeypatch.setattr(collekto_data_fetcher, "authenticate", fake_auth)
    monkeypatch.setattr(collekto_data_fetcher, "get_loan_by_id", lambda t, lid: fallback_loan)
    monkeypatch.setattr(collekto_data_fetcher, "get_disposition_by_id", lambda t, cid: fallback_disp)

    loan_data, disp_data = run_ltfs_flow("baduser", "badpass", "LID", "CID")
    assert calls["count"] == 2
    assert loan_data == fallback_loan
    assert disp_data == fallback_disp


def test_run_ltfs_flow_both_fail(monkeypatch):
    """
    Simulate both primary and fallback authenticate failing => return empty lists.
    """
    def fake_auth_fail(username, password):
        raise AuthenticationError("Always fail")

    monkeypatch.setattr(collekto_data_fetcher, "authenticate", fake_auth_fail)
    # Even if get_loan_by_id were called, they shouldn't be invoked because auth fails twice.
    monkeypatch.setattr(collekto_data_fetcher, "get_loan_by_id", lambda t, lid: {"should": "not be called"})
    monkeypatch.setattr(collekto_data_fetcher, "get_disposition_by_id", lambda t, cid: {"should": "not be called"})

    loan_data, disp_data = run_ltfs_flow("user", "pass", "LID", "CID")
    assert loan_data == []
    assert disp_data == []