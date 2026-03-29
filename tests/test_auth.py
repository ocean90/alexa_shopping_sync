"""Tests for authentication module."""

from __future__ import annotations

import pytest

from custom_components.alexa_shopping_sync.auth import (
    check_page_for_captcha,
    check_page_for_unsupported_flow,
    generate_otp,
    normalize_otp_secret,
    sanitize_log_data,
)
from custom_components.alexa_shopping_sync.exceptions import (
    OTPSecretInvalidError,
    PasskeyDetectedError,
    UnsupportedLoginFlowError,
)


class TestNormalizeOTPSecret:
    """Tests for OTP secret normalization."""

    def test_valid_secret(self):
        # 32-char base32 secret
        secret = "JBSWY3DPEHPK3PXP4AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        result = normalize_otp_secret(secret)
        assert result == secret

    def test_lowercase_to_uppercase(self):
        secret = "jbswy3dpehpk3pxp4aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        result = normalize_otp_secret(secret)
        assert result == secret.upper()

    def test_strips_spaces(self):
        secret = "JBSW Y3DP EHPK 3PXP 4AAA AAAA AAAA AAAA AAAA AAAA AAAA AAA"
        result = normalize_otp_secret(secret)
        assert " " not in result

    def test_strips_dashes(self):
        secret = "JBSWY3DP-EHPK3PXP-4AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        result = normalize_otp_secret(secret)
        assert "-" not in result

    def test_too_short(self):
        with pytest.raises(OTPSecretInvalidError):
            normalize_otp_secret("SHORT")

    def test_invalid_base32(self):
        with pytest.raises(OTPSecretInvalidError):
            normalize_otp_secret("0123456789abcdef0123456789abcdef")


class TestGenerateOTP:
    """Tests for TOTP generation."""

    def test_generates_6_digit_code(self):
        secret = "JBSWY3DPEHPK3PXP"
        code = generate_otp(secret)
        assert len(code) == 6
        assert code.isdigit()


class TestPasskeyDetection:
    """Tests for passkey / unsupported flow detection."""

    def test_passkey_detected(self):
        html = '<div>Use your passkey to sign in</div>'
        with pytest.raises(PasskeyDetectedError):
            check_page_for_unsupported_flow(html)

    def test_passkey_case_insensitive(self):
        html = '<span>This device does not support PASSKEYS</span>'
        with pytest.raises(PasskeyDetectedError):
            check_page_for_unsupported_flow(html)

    def test_fido_detected(self):
        html = '<script>fido.authenticate()</script>'
        with pytest.raises(PasskeyDetectedError):
            check_page_for_unsupported_flow(html)

    def test_webauthn_detected(self):
        html = '<div class="webauthn-container">Sign in with WebAuthn</div>'
        with pytest.raises(PasskeyDetectedError):
            check_page_for_unsupported_flow(html)

    def test_unsupported_flow_claimspicker(self):
        html = '<form id="claimspicker">Select verification method</form>'
        with pytest.raises(UnsupportedLoginFlowError):
            check_page_for_unsupported_flow(html)

    def test_normal_login_page_passes(self):
        html = '<form id="signIn"><input name="email"/><input name="password"/></form>'
        # Should not raise
        check_page_for_unsupported_flow(html)

    def test_captcha_detection(self):
        html = '<div id="auth-captcha-image-container"><img src="captcha.jpg"/></div>'
        assert check_page_for_captcha(html) is True

    def test_no_captcha(self):
        html = '<form id="signIn">Normal login</form>'
        assert check_page_for_captcha(html) is False


class TestSanitizeLogData:
    """Tests for log data sanitization."""

    def test_redacts_password(self):
        data = {"email": "test@test.de", "password": "secret123"}
        result = sanitize_log_data(data)
        assert result["email"] == "test@test.de"
        assert result["password"] == "***REDACTED***"

    def test_redacts_nested(self):
        data = {"auth": {"token": "abc123", "user": "test"}}
        result = sanitize_log_data(data)
        assert result["auth"]["token"] == "***REDACTED***"
        assert result["auth"]["user"] == "test"

    def test_redacts_otp_secret(self):
        data = {"otp_secret": "JBSWY3DP"}
        result = sanitize_log_data(data)
        assert result["otp_secret"] == "***REDACTED***"

    def test_redacts_cookie(self):
        data = {"session_cookie": "abc=123"}
        result = sanitize_log_data(data)
        assert result["session_cookie"] == "***REDACTED***"
