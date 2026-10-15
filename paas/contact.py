"""Single contact address for the whole product (site, legal pages, docs, email Reply-To). Override with PAAS_CONTACT_EMAIL."""
import os, re
CONTACT_EMAIL = (os.environ.get("PAAS_CONTACT_EMAIL") or "sachin@aloof.si").strip()
# role addresses that used to appear in copy; no-reply@ (the From address) is deliberately left alone
_ROLE = re.compile(r"\b(?:privacy|legal|security|hello|sales|support|abuse|billing|contact|press|dpo|gpu|info|help)@aloof\.si\b", re.I)
def apply(text): return _ROLE.sub(lambda m: CONTACT_EMAIL, text)
