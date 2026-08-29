import unittest

from totp import (
    InvalidTOTPSecret,
    generate_totp,
    normalize_totp_secret,
    totp_seconds_remaining,
)


RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"


class TOTPTests(unittest.TestCase):
    def test_rfc_6238_sha1_vector_truncated_to_six_digits(self):
        # RFC 6238's 8-digit value at t=59 is 94287082; the configured
        # six-digit authenticator value is its final six digits.
        self.assertEqual(generate_totp(RFC_SECRET, timestamp=59), "287082")

    def test_secret_normalization_accepts_case_spaces_hyphens_and_missing_padding(self):
        decorated = "  gezd-gnbv gy3t-qojq gezd-gnbv gy3t-qojq  "
        self.assertEqual(normalize_totp_secret(decorated), RFC_SECRET)
        self.assertEqual(generate_totp(decorated, timestamp=59), "287082")

        # This valid Base32 value requires padding, which callers may omit.
        self.assertEqual(normalize_totp_secret("mzxw-6"), "MZXW6===")

    def test_empty_and_invalid_secrets_raise_safe_custom_error(self):
        for secret in ("", "  - -  ", "not_base32!", "A"):
            with self.subTest(secret=secret):
                with self.assertRaises(InvalidTOTPSecret) as raised:
                    generate_totp(secret, timestamp=59)
                if secret:
                    self.assertNotIn(secret, str(raised.exception))
                    self.assertNotIn(secret, repr(raised.exception))

    def test_non_string_secret_is_rejected_with_custom_error(self):
        with self.assertRaises(InvalidTOTPSecret):
            generate_totp(None, timestamp=59)  # type: ignore[arg-type]

    def test_countdown_boundaries(self):
        expected = {
            0: 30,
            1: 29,
            28: 2,
            29: 1,
            30: 30,
            31: 29,
            59: 1,
            60: 30,
        }
        for timestamp, remaining in expected.items():
            with self.subTest(timestamp=timestamp):
                self.assertEqual(totp_seconds_remaining(timestamp), remaining)


if __name__ == "__main__":
    unittest.main()
