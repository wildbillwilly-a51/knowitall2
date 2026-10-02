import unittest

import _support  # noqa: F401

from knowitall2.secrets import contains_secret, find_secrets, redact


class SecretDetectionTests(unittest.TestCase):
    def test_detects_common_secret_shapes(self) -> None:
        # Samples are assembled at runtime so repository secret scanners do not
        # mistake these fake values for leaked credentials.
        samples = {
            "private key": "-----BEGIN " + "OPENSSH PRIVATE " + "KEY-----",
            "GitHub token": "use ghp_" + "a1" * 18,
            "GitLab token": "glpat-" + "b2" * 10,
            "API key": "sk-" + "c3" * 16,
            "AWS access key": "AKIA" + "IOSFODNN7" + "EXAMPLE",
            "URL credentials": "https://admin:" + "hunter22" + "@nas.local/share",
            "authorization header": "curl -H 'Authorization: Bearer " + "abcdefghijklmnop1234'",
            "prose password": "the password is hunter22",
            "prose password in a sentence": "The router admin password is hunter22 for now.",
            "past-tense prose password": "The old password was Tr0ub4dor&3 until today.",
            "assignment": "api_key=abc123def456",
            "connection string": "Server=db;User Id=sa;" + "Password=S3cret!;",
            "JSON web token": ".".join(["eyJ" + "hbGciOiJIUzI1NiJ9", "eyJ" + "zdWIiOiIxMjM0NTY3ODkwIn0", "d" * 43]),
        }
        for label, sample in samples.items():
            with self.subTest(label):
                self.assertTrue(contains_secret(sample), sample)

    def test_allows_references_to_where_credentials_live(self) -> None:
        samples = [
            "The vCenter admin password is in Vaultwarden item vcenter-admin.",
            'The vCenter admin password is in Vaultwarden item "vcenter-admin".',
            "The password is stored in Vaultwarden.",
            "password: stored in Vaultwarden",
            "No saved password is available for this SSID.",
            "The password was recorded in validation output.",
            "The Wi-Fi password is missing from the config.",
            "The admin password was changed last week.",
            "The service password is expired.",
            "api_key=${OPENAI_API_KEY}",
            "api_key=OPENAI_API_KEY",
            "A token is required for the API.",
            "The test pass is flaky on Windows.",
            "Run pwd to print the working directory.",
            "private_key: ~/.ssh/id_ed25519",
            "The router config lives in /etc/config/dhcp on 10.0.0.1.",
            "Commit 58866fa6547696587583065aa5732f880c36e4a2 fixed it.",
            "password: <set during setup>",
            "password=********",
            "ssh://git@gitlab.example.com:2222/group/repo.git",
            "https://gitlab.example.com:443/group/repo",
        ]
        for sample in samples:
            with self.subTest(sample):
                self.assertFalse(contains_secret(sample), find_secrets(sample))

    def test_redact_replaces_only_the_secret(self) -> None:
        redacted = redact("log in with password=hunter22 then check /var/log")
        self.assertNotIn("hunter22", redacted)
        self.assertIn("[REDACTED password]", redacted)
        self.assertIn("then check /var/log", redacted)


if __name__ == "__main__":
    unittest.main()
