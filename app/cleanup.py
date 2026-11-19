"""Remove expired previews and sessions from an initialized application database."""
import json
from datetime import datetime

from sqlalchemy import delete

from .models import Preview, Token


def cleanup_expired(session_factory, now=None):
    cutoff = now if now is not None else datetime.utcnow()
    with session_factory.begin() as db:
        previews = db.execute(delete(Preview).where(Preview.expires <= cutoff))
        sessions = db.execute(delete(Token).where(Token.expires <= cutoff))
        return {'previews': previews.rowcount, 'sessions': sessions.rowcount}


if __name__ == '__main__':
    from .main import SessionLocal

    print(json.dumps(cleanup_expired(SessionLocal)))
