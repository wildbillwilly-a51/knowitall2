import base64
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
KIA_KEY = "kia" + "_" + fake(43, _ALNUM + "-_")  # an agent's key for the KnowItAll2 server
SERVER_CODE = "-".join(fake(4, "ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(5))  # recovery or setup code
JWT = ".".join(["eyJ" + "hbGciOiJIUzI1NiJ9", "eyJ" + "zdWIiOiIxMjM0NTY3ODkwIn0", fake(43)])
# Review 2026-10-11, L-H5.
SHAPED = fake(4, string.ascii_letters) + fake(4, string.digits) + fake(4, string.ascii_letters)
LETTERS = fake(16, string.ascii_lowercase)   # an all-letter Wi-Fi passphrase
_URLSAFE = _ALNUM + "_-"
TAILSCALE_BODY = fake(11) + "CNTRL-" + fake(30)
VAULT_BODY = fake(90, _URLSAFE)
DOCKER_HUB_BODY = fake(27, _URLSAFE)
SENDGRID = "S" + "G." + fake(22, _URLSAFE) + "." + fake(43, _URLSAFE)
DIGITALOCEAN_BODY = fake(64, "0123456789abcdef")
AGE_BODY = fake(58, "QPZRY9X8GF2TVDW0S3JN54KHCE6MUA7L")
DOCKER_AUTH = base64.b64encode(f"builder:{SHAPED}".encode()).decode()
PEM_BASE64 = base64.b64encode(PEM.encode()).decode()
CERTIFICATE_BASE64 = base64.b64encode(("-----BEGIN " + "CERTIFICATE-----\n" + PEM_BODY[0]).encode()).decode()
SESSION = fake(40, "0123456789abcdef")
PUTTY_BODY = fake(64, _BASE64)
PUTTY = ("PuTTY-User-" + "Key-File-3: ssh-ed25519\nEncryption: none\nComment: build\nPublic-Lines: 2\n"
         + fake(64, _BASE64) + "\n" + fake(20, _BASE64) + "\nPrivate-Lines: 1\n" + PUTTY_BODY
         + "\nPrivate-MAC: " + fake(64, "0123456789abcdef"))
BEGIN_RSA = "-----BEGIN " + "RSA PRIVATE " + "KEY-----"
MIXED_CASE = fake(10, string.ascii_lowercase) + fake(10, string.ascii_uppercase)
# An AWS secret's shape: two of each kind of character at least, placed at random.
BARE_AWS = "".join(_RANDOM.sample("Ab3+Zx9/" + fake(32), 40))
HEX64 = fake(64, "0123456789abcdef")
TELEGRAM = fake(10, string.digits) + ":AA" + fake(33, _URLSAFE)

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
    # KnowItAll2's own (audit 2026-10-09): a key file shown in a chat, the key in JSON, the server's codes.
    "KnowItAll2 key file": (f"$ cat ~/.knowitall2/server/keys/claude-code.key\n{KIA_KEY}\n", KIA_KEY[4:]),
    "KnowItAll2 key in JSON": ('{"key": "' + KIA_KEY + '"}', KIA_KEY[4:]),
    "KnowItAll2 key in prose": (f"The agent key for this computer is {KIA_KEY}.", KIA_KEY[4:]),
    "KnowItAll2 recovery code": (f"Recovery code: {SERVER_CODE} (keep it safe)", SERVER_CODE),
    # Review 2026-10-11, L-H5: commands.
    "sqlcmd -P": (f'sqlcmd -S db01 -U sa -P {SHAPED} -Q "select 1"', SHAPED),
    "redis-cli -a": (f"redis-cli -h cache01 -a {SHAPED} ping", SHAPED),
    "net user": (f"net user backupadmin {PASSWORD} /add", PASSWORD),
    "mongosh -p": (f"mongosh --host db01 -u admin -p {SHAPED}", SHAPED),
    "htpasswd -b": (f"htpasswd -b /etc/nginx/.htpasswd admin {SHAPED}", SHAPED),
    "htpasswd -nbB, with no file": (f"htpasswd -nbB admin {SHAPED}", SHAPED),
    "chpasswd": (f"echo 'root:{SHAPED}' | chpasswd", SHAPED),
    "chpasswd here-string": (f'sudo chpasswd <<< "deploy:{SHAPED}"', SHAPED),
    # PowerShell.
    "ConvertTo-SecureString in a pipeline": (f"$pw = '{SHAPED}' | ConvertTo-SecureString -AsPlainText -Force", SHAPED),
    "ConvertTo-SecureString, options first": (f"$s = ConvertTo-SecureString -AsPlainText -Force -String '{SHAPED}'",
                                              SHAPED),
    "-NewPassword": (f"Set-LocalUser -Name svc -NewPassword '{SHAPED}'", SHAPED),
    "-AdminPassword": (f".\\setup.ps1 -AdminPassword {SHAPED}", SHAPED),
    "camelCase ...Pass": (f'$adminPass = "{SHAPED}"', SHAPED),
    # Configuration files.
    "XML password element": (f"<password>{SHAPED}</password>", SHAPED),
    "XML element with a namespace": (f'<wsse:Password Type="PasswordText">{SHAPED}</wsse:Password>', SHAPED),
    "NuGet ClearTextPassword": (f'<add key="ClearTextPassword" value="{SHAPED}" />', SHAPED),
    "unattend.xml Value": (f"<AdministratorPassword>\n  <Value>{SHAPED}</Value>\n  <PlainText>true</PlainText>\n"
                           "</AdministratorPassword>", SHAPED),
    "Docker config.json auth": ('{"auths": {"registry.example.com": {"auth": "' + DOCKER_AUTH + '"}}}', DOCKER_AUTH),
    "wpa_supplicant psk": (f'psk="{SHAPED}"', SHAPED),
    "wpa_supplicant all-letter psk": (f'  psk="{LETTERS}"', LETTERS),
    "OpenWrt uci key": (f"uci set wireless.default_radio0.key='{SHAPED}'", SHAPED),
    "OpenWrt option key": (f"\toption key '{LETTERS}'", LETTERS),
    "OpenWrt option password": (f"\toption password '{SHAPED}'", SHAPED),
    "nmcli password": (f"nmcli dev wifi connect HomeNet password {SHAPED}", SHAPED),
    "nmcli wifi-sec.psk": (f"nmcli con modify HomeNet wifi-sec.psk '{LETTERS}'", LETTERS),
    "Cisco username password 0": (f"username admin privilege 15 password 0 {SHAPED}", SHAPED),
    "Cisco username password": (f"username admin password {SHAPED}", SHAPED),
    "Cisco wpa-psk": (f" wpa-psk ascii 0 {SHAPED}", SHAPED),
    "redis requirepass": (f"requirepass {SHAPED}", SHAPED),
    "redis CONFIG SET requirepass": (f'redis-cli CONFIG SET requirepass "{LETTERS}"', LETTERS),
    # Vendor tokens.
    "Tailscale auth key after --authkey": ("tailscale up --authkey " + "tsk" + "ey-auth-" + TAILSCALE_BODY,
                                           TAILSCALE_BODY),
    "bare Tailscale key": ("Join with " + "tsk" + "ey-auth-" + TAILSCALE_BODY + " before it expires.", TAILSCALE_BODY),
    "Vault token": ("vault login " + "hv" + "s." + VAULT_BODY, VAULT_BODY),
    "Docker Hub token": ("echo " + "dck" + "r_pat_" + DOCKER_HUB_BODY + " | docker login -u builder --password-stdin",
                         DOCKER_HUB_BODY),
    "SendGrid key": (f"The mailer uses {SENDGRID} for now.", SENDGRID[3:]),
    "DigitalOcean token": ("doctl auth init -t " + "do" + "p_v1_" + DIGITALOCEAN_BODY, DIGITALOCEAN_BODY),
    "age secret key": ("AGE-SECRET" + "-KEY-1" + AGE_BODY, AGE_BODY),
    # Other shapes.
    "base64 PEM in a Kubernetes secret": ("data:\n  tls.key: " + PEM_BASE64, PEM_BASE64[40:80]),
    "session cookie": (f"curl -b 'sessionid={SESSION}' https://app.example/api", SESSION),
    "Cookie header": (f"Cookie: theme=dark; sessionid={SESSION}", SESSION),
    "redis URL with no user name": (f"redis://:{SHAPED}@cache01:6379/0", SHAPED),
    "PuTTY private key": (PUTTY, PUTTY_BODY),
    "PEM in Python string pieces": ('key = ("' + BEGIN_RSA + '\\n"\n       "' + PEM_BODY[0] + '\\n"\n       "'
                                    + PEM_BODY[1] + '\\n")', PEM_BODY[1]),
    "PEM in string pieces with real line breaks": ('key = ("' + BEGIN_RSA + '\n"\n       "' + PEM_BODY[2] + '\n")',
                                                   PEM_BODY[2]),
    # Review 2026-10-11, L-L2 and D-L5: what the false alarms below must still catch.
    "a value only starting like an Ansible vault": ("db_password: !vault" + SHAPED, SHAPED),
    "a bearer token of letters in both cases": ("send bearer " + MIXED_CASE + " with each call", MIXED_CASE),
    "a KnowItAll2 code with eight different characters": ("code ABCD-EFGH-ABCD-EFGH-ABCD", "ABCD-EFGH-ABCD"),
    "smbclient with a real password for user": (f"smbclient //nas01/share -U user%{PASSWORD}", PASSWORD),
    "curl with the word pass for another user": ("curl -u admin:pass https://nas01/api", "admin:pass"),
    # Release candidate 0.10.6, R-L1: what the false alarms below must still catch.
    "curl -b after other options": (f"curl -s -L -b 'theme=dark; sessionid={SESSION}' https://app.example/api",
                                    SESSION),
    "curl -b unquoted": (f"curl -b sessionid={SESSION} https://app.example/api", SESSION),
    "curl -b on a continued line": ("curl -X POST https://app.example/api \\\n  -H 'Accept: application/json' \\\n"
                                    f"  -b 'sessionid={SESSION}'", SESSION),
    "--cookie=": (f'./fetch.sh --cookie="sessionid={SESSION}" https://app.example/api', SESSION),
    "a raw 64-digit psk": (f"psk={HEX}{HEX}", HEX),
    "an all-letter psk in a NetworkManager file": (f"[wifi-security]\nkey-mgmt=wpa-psk\npsk={LETTERS}\n", LETTERS),
    "OpenWrt option key in a config block": (f"config wifi-iface 'default_radio0'\n\toption ssid 'Home'\n"
                                             f"\toption key '{SHAPED}'\n\toption encryption 'psk2'\n", SHAPED),
    "OpenWrt option key, unquoted": (f"\toption key {LETTERS}\n", LETTERS),
    "OpenWrt option key quoted in a sentence": (f"The guest network has option key '{LETTERS}' in /etc/config/wireless.",
                                                LETTERS),
    "OpenWrt option key shaped in a sentence": (f"Set option key {SHAPED} for the guest network.", SHAPED),
    # Review 2026-10-24, F-18: shapes the 81-case probe missed.
    "an unlabeled AWS secret": (f"the second value it printed was {BARE_AWS}", BARE_AWS),
    "64 hex characters alone": (f"signing key {HEX64} goes in the env file", HEX64),
    "Slack webhook": ("post to https://hooks.slack" + f".com/services/T{fake(8, string.ascii_uppercase)}/"
                      f"B{fake(10, string.ascii_uppercase)}/{TOKEN[:24]}", TOKEN[:24]),
    "Telegram bot token": (f"bot token {TELEGRAM}", TELEGRAM[-35:]),
    ".pgpass line": (f"db01.lan:5432:*:postgres:{PASSWORD}\n", PASSWORD),
    "psexec -p": (f"psexec \\\\srv01 -u admin -p {SHAPED} cmd", SHAPED),
    "schtasks /rp": (f'schtasks /create /tn backup /ru svc_backup /rp {SHAPED} /sc daily', SHAPED),
    "netsh keyMaterial": (f"netsh wlan set hostednetwork mode=allow ssid=Lab keyMaterial={LETTERS}", LETTERS),
    "WLAN profile keyMaterial": (f"<keyMaterial>{LETTERS}</keyMaterial>", LETTERS),
    "netsh Key Content": (f"    Key Content            : {LETTERS}", LETTERS),
    "a lowercase cookie value": (f"Cookie: sessionid={LETTERS}{LETTERS[:4]}", LETTERS),
    "a one-word prose password": ("the admin password is swordfish", "swordfish"),
    "a one-word prose password ending a quotation": ('He wrote "the NAS password is Kingfisher."', "Kingfisher"),
    "a one-word prose password in a sentence": ("The NAS password is Kingfisher. Change it soon.", "Kingfisher"),
    "prose API token": (f"the API token is {SHAPED}", SHAPED),
    "prose access key": (f"The access key was {TOKEN} until it was rotated.", TOKEN),
}

REFERENCES = [
    # KnowItAll2's own key prefix and code shape, named but not given.
    'KEY_PREFIX = "kia_"',
    "the key file holds kia_ and the key",
    "a recovery code looks like XXXX-XXXX-XXXX-XXXX-XXXX",
    "a join code such as ABCD-EFGH-JKLM-NPQR is used once",
    "see build-step-four-five-six in the runbook",
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
    # Review 2026-10-11, L-H5: the new shapes, without a secret.
    'sqlcmd -S db01 -E -Q "select 1"',
    "sqlcmd -S db01 -U sa -P $env:SQL_PASSWORD",
    "sqlcmd -S db01 -U sa -P <password>",
    "redis-cli -h cache01 -a $REDIS_PASSWORD ping",
    "redis-cli -h cache01 --askpass ping",
    "net user backupadmin * /add",
    "net user backupadmin /delete",
    "Run net user alice to list her groups.",
    "mongosh --host db01 -u admin -p",
    "mongosh --host db01 -u admin -p $MONGO_PASSWORD",
    "htpasswd -c /etc/nginx/.htpasswd admin",
    "htpasswd -b /etc/nginx/.htpasswd admin $HTPASSWD_PASSWORD",
    'echo "$USER_NAME:$NEW_PASSWORD" | chpasswd',
    "cat accounts.txt | chpasswd",
    "$plain | ConvertTo-SecureString -AsPlainText -Force",
    "'<password>' | ConvertTo-SecureString -AsPlainText -Force",
    "ConvertTo-SecureString -AsPlainText -Force -String $plain",
    "Set-LocalUser -Name svc -NewPassword $secure",
    "Set-LocalUser -Name svc -NewPassword (Read-Host -AsSecureString)",
    "Set-ADUser svc -PasswordNeverExpires $true",
    "Set-ADUser svc -CannotChangePassword $true",
    "the --compass flag points north",
    "$adminPass = Read-Host -AsSecureString",
    "$adminPass = $env:ADMIN_PASS",
    "<password>${DB_PASSWORD}</password>",
    "<password>$(DbPassword)</password>",
    "<password>kept in Vaultwarden</password>",
    "<PasswordExpires>never</PasswordExpires>",
    "<label>Password:</label>",
    '<add key="ClearTextPassword" value="%NUGET_PASSWORD%" />',
    '<add key="Username" value="builder" />',
    "<AdministratorPassword>\n  <Value>****</Value>\n</AdministratorPassword>",
    '{"auths": {"registry.example.com": {"auth": ""}}}',
    '{"auth": "basic"}',
    'psk="<passphrase>"',
    "psk=$WIFI_PSK",
    'uci set wireless.default_radio0.key="$WIFI_KEY"',
    "uci set wireless.default_radio0.encryption='psk2'",
    "option key '1'",
    "Set the option key to the passphrase.",
    "nmcli dev wifi connect HomeNet --ask",
    "nmcli asks for the password when it is not given.",
    'nmcli con modify HomeNet wifi-sec.psk "$WIFI_PSK"',
    "username admin privilege 15 secret 9 $9$" + "abcdefghijklmnop",
    "The username and password are in Vaultwarden item 'switch-admin'.",
    "wpa-psk ascii 0 <key>",
    "requirepass ${REDIS_PASSWORD}",
    "Set requirepass in redis.conf to turn on authentication.",
    "tailscale up --authkey file:/etc/tailscale/authkey",
    "tailscale up --authkey $TS_AUTHKEY",
    "Tailscale auth keys start with " + "tsk" + "ey-auth- and expire after 90 days.",
    "Vault service tokens start with " + "hv" + "s. and batch tokens with hvb.",
    "Docker Hub tokens start with " + "dck" + "r_pat_ and are made in the account settings.",
    "A SendGrid key looks like " + "S" + "G.<id>.<secret>.",
    "DigitalOcean tokens start with " + "do" + "p_v1_ and are shown once.",
    "age keys start with " + "AGE-SECRET" + "-KEY-1 and live in keys.txt.",
    "  tls.crt: " + CERTIFICATE_BASE64,
    "curl -b cookies.txt https://app.example/api",
    "Cookie: theme=dark; lang=en",
    'curl -b "sessionid=$SESSION_ID" https://app.example/api',
    "redis://cache01:6379/0",
    "redis://:${REDIS_PASSWORD}@cache01:6379/0",
    "A PuTTY key file keeps the private half under Private-Lines: in the .ppk file.",
    # Review 2026-10-11, L-L2 and D-L5: false alarms.
    "Ansible keeps the database password encrypted inline as `db_password: !vault |` in group_vars/all.yml.",
    "db_password: !vault |\n  $ANSIBLE_VAULT;1.1;AES256\n  " + HEX,
    "The API uses bearer authentication-only tokens, configured in settings.py.",
    "Use `token = response.json()['access_token']` to read the token in the client script.",
    "secret = client.get_secret('db').value",
    "The KnowItAll2 code format is ABCD-ABCD-ABCD-ABCD-ABCD; the real codes are on the server only.",
    "a setup code looks like AAAA-BBBB-CCCC-DDDD-EEEE",
    "smbclient //nas01/share -U user%pass",
    "Give smbclient the account as -U 'WORKGROUP\\user%password'.",
    "curl -u user:pass https://nas01/api",
    "Credentials in a URL look like https://user:password@host/path.",
    # Release candidate 0.10.6, R-L1: false alarms from the new shapes. A -b that is not curl's, or a long
    # name=value later on the line than the cookie argument.
    "git checkout -b fix && npm config set registry=https://registry.npmjs.org/",
    "git checkout -b perf; export NODE_OPTIONS=--max-old-space-size=8192",
    "sort -b -k2 hosts.txt, then open https://app.example/report?view=2026-10-11-weekly-summary",
    "curl -b cookies.txt https://app.example/report?view=2026-10-11-weekly-summary",
    # A psk given a short number, or a 0x number: a WPA key has 8 characters at least.
    "PSK=0x20",
    "psk: 1024",
    "wpa_psk = 0x0002",
    "wpa_psk = 0x00000002",
    "In the controller's SNMP walk, the key-management column is a bitmask: DOT1X=0x10, PSK=0x20, FT-PSK=0x40.",
    # "option key" in prose, not on an OpenWrt config line.
    "use the option key shortcuts",
    "option key shortcuts are listed in the editor's help",
    # Review 2026-10-24, F-18: a test count under a bare "pass", and the new shapes' look-alikes.
    "pass=1078 fail=0 skipped=9",
    "unit tests: pass: 1078, fail: 0",
    "sha256:" + HEX64,
    "The table hash on all four stores is " + HEX64 + ".",
    "SHA-256 of the release tar: " + HEX64,
    "Image " + HEX64 + " is healthy after the deploy.",
    "the cli config has client_id " + HEX64 + "; this app reads it",
    '{"files": {"docs/local-saving.md": "' + HEX64 + '"}}',
    "ap3g3-k9w8-tar.17_15_4_160.tar (" + HEX64.upper() + ")",
    "the host's public key " + HEX64,
    "Frigate healthy: FrigatehealthyunchangedSundayStartedAt10:44:41Z",
    # Codex's review of step A: a repeated placeholder, a row's primary key, a password said to be forgotten.
    "a placeholder such as " + "AB12cd34" * 5 + " goes here",
    "the row's primary key is " + HEX64,
    "the object key " + HEX64 + " names the upload",
    "The admin password is forgotten.",
    'Codex wrote that "The admin password is forgotten." read as a password.',
    'The note says "the password is weak."',
    "The old password was stolen, so it was changed.",
    "C:/Projects/knowitall2/src/knowitall2/Server/Exchange",
    "https://hooks.slack" + ".com/services/T00000000/B00000000/" + "X" * 24,
    "localhost:5432:*:postgres:*",
    "psexec \\\\srv01 -u admin -p $env:ADMIN_PASSWORD cmd",
    "schtasks /create /tn backup /ru SYSTEM /sc daily",
    "schtasks /create /tn backup /ru svc /rp * /sc daily",
    "<keyMaterial>${WIFI_KEY}</keyMaterial>",
    "the admin password is temporary",
    "The password is complicated.",
    "the password is swordfish-like in length",
    "The password is stored elsewhere.",
    "the API token is required for every call",
    "the access key is rotated monthly",
    "the token is valid for 1 hour",
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
            "more commands": ("sqlcmd redis-cli mongosh htpasswd -b nmcli password net user a requirepass "
                              "option key uci set username a wpa-psk ascii 0 chpasswd <<< " * size)[:size],
            "quoted values before pipes": ("'a' |" * size)[:size],
            "XML elements": ("<password>" * size)[:size],
            "XML values": ("<password>a" * size)[:size],
            "key and value attributes": ('key="password" value=' * size)[:size],
            "cookies": ("Cookie: a=b" * size)[:size],
            "user and password pairs": ("a:b|" * size)[:size],
            "base64 PEM": ("LS0tLS1CRUdJTi" * size)[:size],
            "PuTTY keys": ("Private-Lines: 1\n" * size)[:size],
            "empty user names": ("a://:" * size)[:size],
            "blank lines": "\n" * size,
            **{f"repeated {prefix}": (prefix * size)[:size]
               for prefix in ("sk-", "glpat-", "xoxb-", "eyJ-", "pwd=", "kia_", "ABCD-", "tsk" + "ey-",
                              "hv" + "s.", "S" + "G.", "dck" + "r_pat_")},
        }
        for label, text in texts.items():
            with self.subTest(label):
                started = time.perf_counter()
                redact(text)
                self.assertLess(time.perf_counter() - started, 2.0)

    def test_command_forms_are_checked_in_linear_time(self) -> None:
        # Release candidate 0.10.6, R-M4: "htpasswd -" and 16,000 b's took over 3 seconds, and 50,000 took half a
        # minute. Each command form's words and the option before its value, then text built to make a pattern
        # that backtracks read on from every start: the command repeated ("htpasswd -b htpasswd -b ..."), its
        # option repeated, one long option word ("htpasswd -bbb..."), and long runs of spaces or word characters.
        size = 50_000
        forms = {
            "mysql": ("mysql", "-p"),
            "sshpass": ("sshpass", "-p"),
            "curl -u": ("curl", "-u"),
            "curl -b": ("curl", "-b"),
            "--cookie": ("a", "--cookie"),
            "net use": ("net use", "/user:a"),
            "net user": ("net user", "a"),
            ".netrc": ("machine a", "password"),
            "ConvertTo-SecureString": ("ConvertTo-SecureString", "-String"),
            "docker login": ("docker login", "-p"),
            "smbclient": ("smbclient", "-U"),
            "sqlcmd": ("sqlcmd", "-P"),
            "redis-cli": ("redis-cli", "-a"),
            "mongosh": ("mongosh", "-p"),
            "chpasswd": ("chpasswd", "<<<"),
            "Cisco username": ("username a", "password"),
            "Cisco wpa-psk": ("wpa-psk", "ascii"),
            "htpasswd": ("htpasswd", "-b"),
            "nmcli password": ("nmcli", "password"),
            "nmcli psk": ("nmcli", "a.psk"),
            "requirepass": ("requirepass", "a"),
            "OpenWrt option": ("option", "key"),
            "uci set": ("uci set", "a.key="),
        }
        for label, (command, option) in forms.items():
            letter = option.strip("-<./:=")[-1:] or "b"
            texts = {
                "repeated": (f"{command} {option} " * size)[:size],
                "repeated options": (command + f" {option}" * size)[:size],
                "a long option word": f"{command} -" + letter * size + "1",
                "spaces": f"{command} {option}" + " " * size + "1",
                "word characters": f"{command} {option} " + "a" * size,
            }
            for shape, text in texts.items():
                with self.subTest(f"{label}: {shape}"):
                    started = time.perf_counter()
                    redact(text)
                    self.assertLess(time.perf_counter() - started, 0.5)


if __name__ == "__main__":
    unittest.main()
