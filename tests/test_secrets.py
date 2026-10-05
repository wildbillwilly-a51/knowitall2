import random
import string
import time
import unittest

import _support  # noqa: F401

from knowitall2.secrets import contains_secret, find_secrets, redact

# Fake values are generated at runtime, and key markers are split, so
# repository secret scanners do not mistake these tests for leaked credentials.
_RANDOM = random.Random(20261003)
_ALNUM = string.ascii_letters + string.digits
_BASE64 = _ALNUM + "+/"


def fake(length: int, alphabet: str = _ALNUM) -> str:
    return "".join(_RANDOM.choice(alphabet) for _ in range(length))


PASSWORD = fake(4, string.ascii_letters) + "#" + fake(3, string.digits) + fake(4, string.ascii_letters)
TOKEN = fake(32)
HEX = fake(32, "0123456789abcdef")
AWS_SECRET = fake(40, _BASE64)
AZURE_KEY = fake(86, _BASE64) + "=="
PEM_BODY = [fake(64, _BASE64) for _ in range(3)]
PEM = "\n".join(["-----BEGIN " + "RSA PRIVATE " + "KEY-----", *PEM_BODY, "-----END " + "RSA PRIVATE " + "KEY-----"])
GCP_PEM = "\\n".join(["-----BEGIN " + "PRIVATE " + "KEY-----", *PEM_BODY, "-----END " + "PRIVATE " + "KEY-----", ""])
JWT = ".".join(["eyJ" + "hbGciOiJIUzI1NiJ9", "eyJ" + "zdWIiOiIxMjM0NTY3ODkwIn0", fake(43)])

# label: (text, the fake secret in it, which must not survive redaction)
SECRETS = {
    # Shapes caught before the 2026-10-02 review.
    "private key": ("-----BEGIN " + "OPENSSH PRIVATE " + "KEY-----", "OPENSSH PRIVATE"),
    "GitHub token": ("use ghp_" + "a1" * 18, "a1" * 18),
    "GitHub fine-grained token": ("github_pat_" + fake(22) + "_" + fake(59), "github_pat_"),
    "GitLab token": ("glpat-" + "b2" * 10, "b2" * 10),
    "API key": ("sk-" + "c3" * 16, "c3" * 16),
    "Anthropic key": ("sk-ant-api03-" + TOKEN, TOKEN),
    "AWS access key": ("AKIA" + "IOSFODNN7" + "EXAMPLE", "IOSFODNN7"),
    "Slack token": ("xoxb-" + fake(12, string.digits) + "-" + TOKEN, TOKEN),
    "URL credentials": ("https://admin:" + "hunter22" + "@nas.local/share", "hunter22"),
    "authorization header": ("curl -H 'Authorization: Bearer " + "abcdefghijklmnop1234'", "abcdefghijklmnop1234"),
    "prose password": ("the password is hunter22", "hunter22"),
    "prose password in a sentence": ("The router admin password is hunter22 for now.", "hunter22"),
    "past-tense prose password": ("The old password was Tr0ub4dor&3 until today.", "Tr0ub4dor"),
    "assignment": ("api_key=abc123def456", "abc123def456"),
    "password assignment": (f"password={PASSWORD}", PASSWORD),
    "password with a colon": (f"password: {PASSWORD}", PASSWORD),
    "connection string": ("Server=db;User Id=sa;" + "Password=S3cret!;", "S3cret"),
    "connection string with a value": (f"Server=db;Password={PASSWORD};", PASSWORD),
    "JSON web token": (JWT, JWT[-43:]),
    # Key names with a prefix, a suffix, or glued on (review finding H3).
    "DB_PASSWORD": (f"DB_PASSWORD={PASSWORD}", PASSWORD),
    "AWS_SECRET_ACCESS_KEY": (f"AWS_SECRET_ACCESS_KEY={AWS_SECRET}", AWS_SECRET),
    "AWS credentials file": (f"[default]\naws_access_key_id = AKIA{fake(16, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567')}\n"
                             f"aws_secret_access_key = {AWS_SECRET}", AWS_SECRET),
    "POSTGRES_PASSWORD in YAML": (f"POSTGRES_PASSWORD: {PASSWORD}", PASSWORD),
    "PGPASSWORD": (f"PGPASSWORD={PASSWORD} psql -h db -U app", PASSWORD),
    "MYSQL_PWD": (f"MYSQL_PWD={PASSWORD}", PASSWORD),
    "SECRET_KEY": (f"SECRET_KEY=django-insecure-{TOKEN}", TOKEN),
    "OPENAI_API_KEY without the sk- prefix": (f"OPENAI_API_KEY={HEX}", HEX),
    "GITLAB_TOKEN": (f"GITLAB_TOKEN={TOKEN[:20]}", TOKEN[:20]),
    "export API_TOKEN": (f"export API_TOKEN={TOKEN}", TOKEN),
    "bare token": (f"token={TOKEN}", TOKEN),
    "quoted secret_key": (f'secret_key = "{TOKEN}"', TOKEN),
    "spring property": (f"spring.datasource.password={PASSWORD}", PASSWORD),
    "x-api-key header": (f"x-api-key: {TOKEN}", TOKEN),
    "token in a URL query": (f"https://api.example.com/v1?access_token={TOKEN}&x=1", TOKEN),
    # JSON, camelCase, and other languages' assignments.
    "JSON password": ('{"password": "' + PASSWORD + '"}', PASSWORD),
    "JSON access_token": ('{"access_token": "' + TOKEN + '", "expires_in": 3600}', TOKEN),
    "camelCase key": ('{dbPassword: "' + PASSWORD + '"}', PASSWORD),
    "Go assignment": (f'clientSecret := "{TOKEN}"', TOKEN),
    "PowerShell variable": (f"$password = '{PASSWORD}'", PASSWORD),
    # Values that start with a character the old rules took for a reference.
    "value starting with $": ("password=$uperS3cret!", "uperS3cret"),
    "value starting with *": ("password=*Xk9mQ2vL8", "Xk9mQ2vL8"),
    "value starting with (": ("password=(Xk9mQ2vL8)", "Xk9mQ2vL8"),
    "value starting with ~": ("password=~Xk9mQ2vL8", "Xk9mQ2vL8"),
    "value starting with [": ("password=[Xk9mQ2vL8]", "Xk9mQ2vL8"),
    "value starting with %": ("password=%Xk9mQ2vL8", "Xk9mQ2vL8"),
    "base64 value starting with /": (f"secret_key=/{TOKEN[:20]}+{TOKEN[20:]}=", TOKEN[:20]),
    # The whole private key, not only its first line.
    "PEM block": (PEM, PEM_BODY[1]),
    "service-account JSON": ('{"type": "service_account", "private_key": "' + GCP_PEM
                             + '", "client_email": "x@p.iam.gserviceaccount.com"}', PEM_BODY[2]),
    # Vendor prefixes.
    "Stripe key": ("sk_" + "live_" + TOKEN[:24], TOKEN[:24]),
    "npm token": ("npm_" + fake(36), "npm_"),
    "npm _authToken": (f"//registry.npmjs.org/:_authToken={TOKEN}", TOKEN),
    "Hugging Face token": ("hf_" + fake(34), "hf_"),
    "Azure AccountKey": (f"DefaultEndpointsProtocol=https;AccountName=acct;AccountKey={AZURE_KEY};"
                         "EndpointSuffix=core.windows.net", AZURE_KEY[:40]),
    "Azure SharedAccessKey": ("Endpoint=sb://ns.servicebus.windows.net/;SharedAccessKeyName=Root;"
                              f"SharedAccessKey={AZURE_KEY[:43]}=", AZURE_KEY[:43]),
    # Credentials in URLs, with '@' and '/' in the password.
    "URL password with @": ("postgres://admin:p@ssw0rd@db:5432/app", "ssw0rd"),
    "URL password with /": ("https://user:ab/cd123@host/x", "cd123"),
    "URL password that is a word": ("https://user:secret@gitlab.example.com/group/repo/", "secret"),
    # Command lines.
    "--password value": (f"mysql -u root --password {PASSWORD} app", PASSWORD),
    "--password=value": (f"mysql -u root --password={PASSWORD} app", PASSWORD),
    "mysql -p": (f"mysql -u root -p{PASSWORD} app", PASSWORD),
    "sshpass -p": (f"sshpass -p {PASSWORD} ssh root@nas01", PASSWORD),
    "sshpass -p word": ("sshpass -p secret ssh root@nas01", "secret"),
    "curl -u": (f"curl -u admin:{PASSWORD} https://nas01/api", PASSWORD),
    "net use": (f"net use Z: \\\\nas01\\share /user:admin {PASSWORD}", PASSWORD),
    ".netrc on one line": (f"machine nas01 login admin password {PASSWORD}", PASSWORD),
    ".netrc over lines": (f"machine nas01\n  login admin\n  password {PASSWORD}", PASSWORD),
    "ConvertTo-SecureString": (f"$s = ConvertTo-SecureString '{PASSWORD}' -AsPlainText -Force", PASSWORD),
    "openssl -passin": (f"openssl rsa -in k.pem -passin pass:{PASSWORD}", PASSWORD),
    # Natural language.
    "password for a host": (f"the root password for nas01 is {PASSWORD}", PASSWORD),
    "prose wrapped across lines": ("The router admin password\nis hunter22 for now.", "hunter22"),
    "dotted value": ("password=Xk9." + "mQ2vL8pR", "mQ2vL8pR"),
    "a password that is the word password": ("The camera's default password is password.", "is password"),
    "a factory password": ("the default password is admin", "is admin"),
    "a quoted all-letter password": ("The password is 'abcdefgh'.", "abcdefgh"),
    # Review 2026-10-04, L-L3.
    "docker login -p": (f"docker login -u alex -p {PASSWORD} registry.example", PASSWORD),
    "smbclient user%password": (f"smbclient //nas01/share -U alex%{PASSWORD}", PASSWORD),
}

REFERENCES = [
    # How people describe a password, not the password (review 2026-10-04, C-M1).
    "The password is shared with the team.",
    "The admin password is different on each node.",
    "The password is case-sensitive.",
    "The password was emailed to the new hire.",
    "The password is twelve characters long.",
    "The Wi-Fi password is per-user.",
    "The password was leaked last year, so it was changed.",
    # Code that gets a value (C-L1, L-L2).
    "password = getpass()",
    "Run `pwd = Path.cwd()` to get the folder.",
    # Where a credential is kept, which is what KnowItAll2 stores instead.
    "The vCenter admin password is in Vaultwarden item vcenter-admin.",
    'The vCenter admin password is in Vaultwarden item "vcenter-admin".',
    "The password is stored in Vaultwarden.",
    "password: stored in Vaultwarden",
    "No saved password is available for this SSID.",
    "The password was recorded in validation output.",
    "The Wi-Fi password is missing from the config.",
    "The admin password was changed last week.",
    "The service password is expired.",
    "use the secret_key setting in Vaultwarden",
    "password is in Vaultwarden item 'nas01 root'",
    "DB_PASSWORD is in Vaultwarden item 'db'",
    "The root password for nas01 is in Vaultwarden item 'nas01 root'.",
    "The password for nas01 is stored in KeePass.",
    "the password for the admin account is required",
    "the password for nas01 is weak",
    'The note says "the password for nas01 is weak".',
    "PGPASSWORD is set from ~/.pgpass",
    "secret: see the vault",
    # Variables, templates, paths, masks, and earlier redactions.
    "api_key=${OPENAI_API_KEY}",
    "api_key=OPENAI_API_KEY",
    "${DB_PASSWORD}",
    "DB_PASSWORD=${DB_PASSWORD}",
    "POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}",
    "spring.datasource.password=${DB_PASS}",
    "$env:DB_PASSWORD",
    "$env:DB_PASSWORD = $plain",
    "password=$env:DB_PASSWORD",
    "%PASSWORD%",
    "set PASSWORD=%PASSWORD%",
    "<password>",
    "password=<password>",
    "password: <set during setup>",
    "x-api-key: <your key>",
    "csrf_token = {{ csrf_token }}",
    "****",
    "password=****",
    "password=********",
    "private_key: ~/.ssh/id_ed25519",
    "SECRET_KEY_FILE=/run/secrets/django",
    "secret: /run/secrets/django",
    "token=/var/lib/app/token.txt",
    "machine nas01 login admin password *",
    "the password: [REDACTED password]",
    "log in with password=[REDACTED password] then",
    "curl -u admin:[REDACTED password] https://x",
    "--password [REDACTED password]",
    # Code.
    "def create(store: Store, username: object, new_password: object, *, now: str) -> str:",
    "return sorted(counts, key=lambda token: (-counts[token], token))[:limit]",
    '{"username": "keeper", "password": PASSWORD})',
    "secret_key = 'changeme'",
    "$password = Read-Host -AsSecureString",
    "ConvertTo-SecureString $plain -AsPlainText -Force",
    "secret = events['A candidate that contained a secret; nothing of it was kept.']",
    'check(store, ("new_password: object"))',
    "reasons = {'secret': 'it contained a password or key'}",
    "await api('POST', 'setup', { new_password: data.password });",
    "password: process.env.DB_PASSWORD",
    'environment = {"PGPASSWORD": password} if password else {}; run(f"PGPASSWORD={password} psql")',
    'url = f"postgres://app:{password}@db/app"',
    "DB_PASSWORD = os.environ['DB_PASSWORD']",
    'SECRET_KEY = os.getenv("SECRET_KEY", "")',
    "if len(candidate) > MAX_PASSWORD:\n        raise AdminError('too long')",
    "if token:\n    store.connection.execute('DELETE FROM sessions')",
    # Settings and prose that only mention a secret word.
    "the default password policy requires 12 characters",
    "Default password policy: 12 characters",
    "the password policy requires 12 characters",
    "set PASSWORD_MIN_LENGTH=12",
    "Password reset link expires in 24 hours.",
    "password_hash: argon2id",
    "enable_secret: true",
    "next_page_token: null",
    "the token is valid for 1 hour",
    "the token is required",
    "A token is required for the API.",
    "access_token expires; refresh_token: rotated nightly",
    "OAuth uses the access_token and refresh_token fields",
    "token budget",
    "the token budget is 4000",
    "Tokens: 1500 used",
    "token: refreshed",
    "MAX_TOKENS=4096",
    "max_tokens: 4096",
    "MAX_TOKEN=4096",
    "TOKEN_URL=https://login.example.com/oauth/token",
    "SECRET_NAME=prod-db",
    "API_KEY_HEADER=X-Api-Key",
    "bypass = enabled",
    "pass_rate=0.95",
    "The test pass is flaky on Windows.",
    "Run pwd to print the working directory.",
    # Command lines without a secret.
    "docker login -u alex --password-stdin < pw.txt",
    "use --password to pass it on the command line",
    "the --password flag prompts when empty",
    "mysql -u root -p app",
    "docker run -p 8080:80 nginx",
    "ssh -p 2222 root@nas01",
    "mkdir -p /srv/data",
    "curl -u admin https://nas01:8443/api",
    "net use Z: \\\\nas01\\share /user:admin *",
    "net use Z: \\\\nas01\\share /user:admin /persistent:yes",
    # Addresses: a port or a user name is not a password.
    "https://oauth2:${GITLAB_TOKEN}@gitlab.example.com/group/repo.git",
    "https://gitlab.example.com:443/users/alex@example.com",
    "ssh://git@gitlab.example.com:2222/group/repo.git",
    "https://gitlab.example.com:443/group/repo",
    "git@github.com:org/repo.git",
    "The router config lives in /etc/config/dhcp on 10.0.0.1.",
    "Commit 58866fa6547696587583065aa5732f880c36e4a2 fixed it.",
]


class SecretDetectionTests(unittest.TestCase):
    def test_detects_common_secret_shapes(self) -> None:
        for label, (sample, secret) in SECRETS.items():
            with self.subTest(label):
                self.assertIn(secret, sample)
                self.assertTrue(contains_secret(sample), sample)
                self.assertNotIn(secret, redact(sample))

    def test_allows_references_to_where_credentials_live(self) -> None:
        for sample in REFERENCES:
            with self.subTest(sample):
                self.assertFalse(contains_secret(sample), find_secrets(sample))

    def test_redacted_text_is_clean_and_redacting_again_changes_nothing(self) -> None:
        # Learning quotes redacted text as evidence, which is checked for secrets again.
        for label, (sample, _) in SECRETS.items():
            with self.subTest(label):
                once = redact(sample)
                self.assertFalse(contains_secret(once), (once, find_secrets(once)))
                self.assertEqual(once, redact(once))

    def test_redact_replaces_only_the_secret(self) -> None:
        redacted = redact("log in with password=hunter22 then check /var/log")
        self.assertNotIn("hunter22", redacted)
        self.assertIn("[REDACTED password]", redacted)
        self.assertIn("then check /var/log", redacted)

    def test_markers_name_the_kind_of_secret(self) -> None:
        samples = {
            f"password={PASSWORD}": "password=[REDACTED password]",
            f"DB_PASSWORD={PASSWORD}": "DB_PASSWORD=[REDACTED password]",
            "api_key=abc123def456": "api_key=[REDACTED API key]",
            "uses sk-" + "c3" * 16: "uses [REDACTED API key]",
            f"token={TOKEN}": "token=[REDACTED token]",
            f'clientSecret := "{TOKEN}"': "clientSecret := [REDACTED secret]",
            f"AWS_SECRET_ACCESS_KEY={AWS_SECRET}": "AWS_SECRET_ACCESS_KEY=[REDACTED secret]",
            f"key:\n{PEM}\nend": "key:\n[REDACTED private key]\nend",
            "postgres://admin:p@ssw0rd@db:5432/app": "[REDACTED credentials in a URL]db:5432/app",
        }
        for sample, expected in samples.items():
            with self.subTest(sample):
                self.assertEqual(expected, redact(sample))

    def test_a_url_password_may_hold_at_signs_and_slashes_but_a_port_is_not_one(self) -> None:
        self.assertEqual("[REDACTED credentials in a URL]host/x", redact("https://user:ab/cd123@host/x"))
        self.assertEqual("see [REDACTED credentials in a URL]db.local:5432/app now",
                         redact("see postgres://app:p@ss@w0rd@db.local:5432/app now"))
        for sample in ("https://gitlab.example.com:443/users/alex@example.com",
                       "https://oauth2:${GITLAB_TOKEN}@gitlab.example.com/group/repo.git",
                       "https://oauth2:$GITLAB_TOKEN@gitlab.example.com/group/repo.git",
                       "https://oauth2:%GITLAB_TOKEN%@gitlab.example.com/group/repo.git",
                       "https://oauth2:$env:GITLAB_TOKEN@gitlab.example.com/group/repo.git"):
            with self.subTest(sample):
                self.assertFalse(contains_secret(sample), find_secrets(sample))

    def test_shell_variables_are_references_but_dollar_values_are_not(self) -> None:
        for sample in ("password=$DB_PASSWORD", "password=$env:DB_PASSWORD", "password=%DB_PASSWORD%",
                       "$env:DB_PASSWORD = $plain"):
            with self.subTest(sample):
                self.assertFalse(contains_secret(sample), find_secrets(sample))
        for sample in ("password=$uperS3cret!", "password=$3cr3tPass", "password=%Xk9mQ2vL8"):
            with self.subTest(sample):
                self.assertTrue(contains_secret(sample), sample)

    def test_long_texts_are_checked_quickly(self) -> None:
        # A key-name pattern that backtracks takes seconds on 10,000 characters of these.
        size = 100_000
        for label, text in {"identifier characters": ("a_" * size)[:size],
                            "base64url": fake(size, _ALNUM + "-_")}.items():
            with self.subTest(label):
                started = time.perf_counter()
                redact(text)
                self.assertLess(time.perf_counter() - started, 1.0)

    def test_repeated_shapes_are_checked_in_linear_time(self) -> None:
        # Each would take several seconds if a pattern read on to the end of the text from every start.
        size = 50_000
        texts = {
            "assignments": ("a:" * size)[:size],
            "a query string": ("a=1&" * size)[:size],
            "secret keys": ("token=" * size)[:size],
            "references": ("password=<" * size)[:size],
            "flags": ("--password=-" * size)[:size],
            "dotted words": ("a." * size)[:size],
            "dashed words": ("-a" * size)[:size],
            "address-like": ("a://b:c" * size)[:size],
            "commands": ("curl sshpass mysql net use " * size)[:size],
            "blank lines": "\n" * size,
            **{f"repeated {prefix}": (prefix * size)[:size] for prefix in ("sk-", "glpat-", "xoxb-", "eyJ-", "pwd=")},
        }
        for label, text in texts.items():
            with self.subTest(label):
                started = time.perf_counter()
                redact(text)
                self.assertLess(time.perf_counter() - started, 2.0)


if __name__ == "__main__":
    unittest.main()
