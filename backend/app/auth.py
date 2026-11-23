from dataclasses import dataclass, field
from threading import Lock
from uuid import UUID, uuid4

from app.conditions import QuestionContext
from app.schemas import AnalysisResponse, Evidence


@dataclass(frozen=True)
class Account:
    account_id: str
    display_name: str
    state: str | None = None

    def allows(self, state: str | None, *, nationwide: bool = False) -> bool:
        return self.state is None or (not nationwide and state in (None, self.state))


DEMO_ACCOUNTS = {
    "demo-hq": Account("demo-hq", "总部运营"),
    "demo-sp": Account("demo-sp", "圣保罗州运营", "SP"),
}


@dataclass
class Session:
    session_id: UUID
    account: Account
    dataset_id: UUID
    context: QuestionContext = field(default_factory=QuestionContext)
    awaiting_clarification: bool = False
    analyses: dict[UUID, AnalysisResponse] = field(default_factory=dict)
    evidence: dict[UUID, Evidence] = field(default_factory=dict)
    lock: Lock = field(default_factory=Lock, repr=False)

    def get_evidence(self, evidence_id: UUID) -> Evidence:
        item = self.evidence.get(evidence_id)
        if item is None or item.result.dataset_id != self.dataset_id:
            raise LookupError("Evidence not found")
        if self.account.state is not None and item.result.state != self.account.state:
            raise LookupError("Evidence not found")
        return item


class SessionStore:
    def __init__(self) -> None:
        self._sessions: dict[UUID, Session] = {}
        self._lock = Lock()

    def create(self, account: Account, dataset_id: UUID) -> Session:
        session = Session(uuid4(), account, dataset_id)
        with self._lock:
            self._sessions[session.session_id] = session
        return session

    def get(self, account: Account, session_id: UUID) -> Session:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None or session.account != account:
            raise LookupError("Session not found")
        return session
