from app import dotenv as _dotenv

_dotenv.load()        # must run before any module reads os.environ (auth.py, agent.py)
