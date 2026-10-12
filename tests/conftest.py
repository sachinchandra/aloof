import os, sys, tempfile
from cryptography.fernet import Fernet
_d = tempfile.mkdtemp(prefix="aloof-test-")
os.environ.update(PAAS_DB=os.path.join(_d, "t.sqlite"), PAAS_SECRET_KEY=Fernet.generate_key().decode(), PAAS_BASE_DOMAIN="aloof.test",
                  PAAS_PUBLIC_API_URL="http://api.aloof.test", RAZORPAY_KEY_ID="rzp_test_X", RAZORPAY_KEY_SECRET="ks_secret",
                  RAZORPAY_WEBHOOK_SECRET="wh_secret", PAAS_SELLER_STATE="Karnataka", PAAS_SELLER_GSTIN="29ABCDE1234F1Z5",
                  PAAS_EDGE_IPS="203.0.113.10", STRIPE_SECRET_KEY="sk_test_X", STRIPE_WEBHOOK_SECRET="whsec_test",
                  STRIPE_PRICE_HOBBY="price_hobby", STRIPE_PRICE_PRO="price_pro", PAAS_OUTBOX=os.path.join(_d, "outbox.log"))
os.environ.pop("PAAS_DB_URL", None); os.environ.pop("STRIPE_TAX", None); os.environ.pop("PAAS_ENV_FILE", None); os.environ.pop("PAAS_RAZORPAY_ENABLED", None)
for k in [k for k in os.environ if k.startswith(("E2E_", "JARVISLABS_", "KRUTRIM_", "AWS_", "SMTP_"))]: os.environ.pop(k)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
