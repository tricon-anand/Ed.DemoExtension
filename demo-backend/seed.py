from app.db import SessionLocal, Base, engine
from app import models as m
from pwdlib import PasswordHash

Base.metadata.create_all(bind=engine)
db = SessionLocal()

password_hash = PasswordHash.recommended()

def get_password_hash(password: str) -> str:
    return password_hash.hash(password)

org = m.Organization(name="Test Org")
user = m.User(org=org, email="anand@email.com", password_hash=get_password_hash("1234"), role="admin")
sc = m.Scenario(org=org, name="Retail demo", target_host="localhost")
sc.rules = [
    m.Rule(selector="#company-name", type="text", value="{{company}}", position=0),
    m.Rule(selector="#revenue", type="text", value="{{currency}}{{revenue}}", position=1),
]
sc.profiles = [
    m.Profile(name="UK", variables={"company": "Northbridge Retail Ltd", "currency": "£", "revenue": "1,980,500"}),
    m.Profile(name="India", variables={"company": "Bharat Retail Pvt Ltd", "currency": "₹", "revenue": "8,45,20,000"}),
]
db.add_all([org, user, sc])
db.commit()

# read it back
s = db.query(m.Scenario).first()
print(s.name, "->", len(s.rules), "rules,", [p.name for p in s.profiles])