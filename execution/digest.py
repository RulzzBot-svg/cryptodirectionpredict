"""Once-a-day P/L scorecard so a busy operator does not need Render SQL."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import select

from models.prediction import PredictionBet, PredictionBankroll


@dataclass(frozen=True)
class Slice:
    n: int
    wins: int
    pnl: float

    @property
    def wr(self) -> float:
        return (self.wins / self.n) if self.n else 0.0


@dataclass(frozen=True)
class DailyDigest:
    local_day: date
    tz_name: str
    live: bool
    auto_bet: bool
    haircut: float
    bank: float
    vaulted: float
    full: Slice
    since_haircut: Slice
    today: Slice
    last50: Slice
    halt_detail: Optional[str]
    blackout_label: str

    @property
    def all_in(self) -> float:
        return self.bank + self.vaulted

    def call_line(self) -> str:
        if self.halt_detail:
            return f"HALTED — {self.halt_detail}"
        if self.live:
            return "LIVE armed — same $5 / 60–74 / halt+blackout"
        if self.since_haircut.n < 40:
            return (
                f"STAY PAPER — haircut sample {self.since_haircut.n} "
                f"(need ~40–50 before live)"
            )
        if (
            self.today.n >= 25
            and self.today.wr <= 0.56
            and self.today.pnl < 0
        ) or (self.today.n >= 8 and self.today.pnl <= -15):
            return "STAY PAPER — today looks like a kill day"
        return "PAPER sample OK — live is still a human env flip"


def _as_utc(raw: object) -> Optional[datetime]:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        dt = raw
    else:
        text = str(raw).strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _slice_of(rows: list[tuple[float, int]]) -> Slice:
    n = len(rows)
    wins = sum(1 for _pnl, won in rows if won)
    pnl = sum(pnl for pnl, _won in rows)
    return Slice(n=n, wins=wins, pnl=float(pnl))


def digest_due(
    *,
    tz_name: str,
    hour: int,
    last_sent: Optional[date],
    now: Optional[datetime] = None,
) -> bool:
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    local = instant.astimezone(ZoneInfo(tz_name))
    if local.hour < hour:
        return False
    return last_sent != local.date()


def read_stamp(path: Path) -> Optional[date]:
    try:
        text = path.read_text(encoding="utf-8").strip()
        return date.fromisoformat(text) if text else None
    except (OSError, ValueError):
        return None


def write_stamp(path: Path, day: date) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(day.isoformat() + "\n", encoding="utf-8")


def _parse_db_path(database_url: str) -> Path:
    raw = database_url or ""
    if raw.startswith("sqlite:///"):
        return Path(raw[len("sqlite:///") :])
    return Path(raw)


def digest_stamp_path(database_url: str) -> Path:
    return _parse_db_path(database_url).with_name(
        _parse_db_path(database_url).name + ".digest_date"
    )


def load_digest(
    session_factory,
    *,
    tz_name: str,
    haircut_since: datetime,
    live: bool,
    auto_bet: bool,
    haircut: float,
    halt_detail: Optional[str],
    blackout_label: str,
    now: Optional[datetime] = None,
) -> DailyDigest:
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    tz = ZoneInfo(tz_name)
    today = instant.astimezone(tz).date()
    since = haircut_since
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)

    with session_factory() as session:
        bank_row = session.scalars(
            select(PredictionBankroll).order_by(PredictionBankroll.id.asc())
        ).first()
        bank = float(bank_row.usd_balance) if bank_row is not None else 0.0
        vaulted = float(getattr(bank_row, "vaulted_usd", 0.0) or 0.0) if bank_row else 0.0
        bets = session.execute(
            select(
                PredictionBet.pnl,
                PredictionBet.settled_at,
                PredictionBet.status,
            )
            .where(PredictionBet.status.in_(("WON", "LOST")))
            .order_by(PredictionBet.settled_at)
        ).all()

    parsed: list[tuple[float, int, datetime]] = []
    for pnl, settled_at, status in bets:
        when = _as_utc(settled_at)
        if when is None:
            continue
        won = 1 if str(status).upper() == "WON" or float(pnl or 0.0) > 0 else 0
        parsed.append((float(pnl or 0.0), won, when))

    full_rows = [(p, w) for p, w, _t in parsed]
    since_rows = [(p, w) for p, w, t in parsed if t >= since]
    today_rows = [(p, w) for p, w, t in parsed if t.astimezone(tz).date() == today]
    last50 = full_rows[-50:]

    return DailyDigest(
        local_day=today,
        tz_name=tz_name,
        live=live,
        auto_bet=auto_bet,
        haircut=haircut,
        bank=bank,
        vaulted=vaulted,
        full=_slice_of(full_rows),
        since_haircut=_slice_of(since_rows),
        today=_slice_of(today_rows),
        last50=_slice_of(last50),
        halt_detail=halt_detail,
        blackout_label=blackout_label,
    )


def format_digest(d: DailyDigest) -> str:
    def line(label: str, s: Slice) -> str:
        if s.n == 0:
            return f"{label}: n=0"
        return f"{label}: n={s.n} wr={s.wr*100:.1f}% pnl=${s.pnl:+.2f}"

    live = "ON" if d.live else "OFF"
    auto = "ON" if d.auto_bet else "OFF"
    halt = d.halt_detail or "off"
    return "\n".join(
        [
            f"DAILY {d.local_day.isoformat()} {d.tz_name}",
            f"LIVE {live} | AUTO_BET {auto} | haircut {d.haircut:.2f}",
            (
                f"Bank ${d.bank:,.2f} | Vault ${d.vaulted:,.2f} | "
                f"All-in ${d.all_in:,.2f}"
            ),
            line("FULL", d.full),
            line("SINCE_HAIRCUT", d.since_haircut),
            line("TODAY", d.today),
            line("LAST50", d.last50),
            f"Halt: {halt}",
            f"Blackout: {d.blackout_label}",
            f"Call: {d.call_line()}",
        ]
    )
