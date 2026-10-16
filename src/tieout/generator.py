"""Seeded synthetic data generator for one month of bank statement and GL cash activity.

Everything is fictional. The generator plants a configurable number of each break type and writes
ground truth to labels.json. Every planted break leaves evidence an accountant could use:

* DEPOSIT_IN_TRANSIT: GL receipt dated in the last two business days with no bank deposit.
* OUTSTANDING_CHECK: GL check in our check series, issued late in the month, not cleared.
* BANK_FEE_UNRECORDED / INTEREST_UNRECORDED: standard bank transaction descriptions.
* NSF_RETURN_UNRECORDED: "RETURNED ITEM NSF" line citing the customer check number of an earlier
  deposit that is in the GL.
* GL_DUPLICATE: second GL posting of the same invoice and ACH trace one or two days later.
* GL_TRANSPOSITION: GL amount is the bank amount with two adjacent digits swapped; same payee and
  check number or trace.
* GL_WRONG_AMOUNT: GL memo shows the invoice amount, which equals the bank amount; the booked
  amount differs and is not a digit swap.
* BANK_ERROR: bank line carrying a bank adjustment notice reference (ADJN...) for an item that
  does not belong to this account.

Money is generated as integer cents, never floats.
"""

from __future__ import annotations

import calendar
import csv
import json
import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from tieout.models import BreakType
from tieout.money import ZERO, cents_to_money, money_to_cents, to_money

DEFAULT_BREAKS: dict[BreakType, int] = {
    BreakType.DEPOSIT_IN_TRANSIT: 3,
    BreakType.OUTSTANDING_CHECK: 4,
    BreakType.BANK_FEE_UNRECORDED: 3,
    BreakType.INTEREST_UNRECORDED: 1,
    BreakType.NSF_RETURN_UNRECORDED: 2,
    BreakType.GL_DUPLICATE: 2,
    BreakType.GL_TRANSPOSITION: 2,
    BreakType.GL_WRONG_AMOUNT: 2,
    BreakType.BANK_ERROR: 2,
}

VENDORS = [
    "Brightline Office Supply",
    "Granite Peak Logistics",
    "Harborview Mechanical",
    "Silverleaf Packaging",
    "Northgate Electric",
    "Copperfield Equipment Rental",
    "Bluewater Janitorial Services",
    "Redwood Consulting Group",
    "Summit Ridge Engineering",
    "Lakeshore Fleet Services",
    "Ironbridge Manufacturing",
    "Meadowbrook Catering",
    "Pinecrest Software Solutions",
    "Stonegate Properties",
    "Riverbend Freight Systems",
    "Clearwater Laboratories",
    "Foxglove Marketing Partners",
    "Oakhaven Insurance Associates",
    "Trailhead Industrial Supply",
    "Kestrel Technologies",
    "Wildflower Print Management",
    "Bramblewood Distribution",
    "Cobalt Transport Company",
    "Juniper Security Systems",
    "Maplestone Waste Management",
    "Driftwood Telecom Services",
    "Thornbury Legal Associates",
    "Sagebrush Fuel Distribution",
    "Highmark Staffing Partners",
    "Larkspur Engineering Services",
    "Ember Valley Utilities",
    "Quillfeather Translation Services",
    "Tidewater Equipment Company",
    "Windmere Facilities Management",
    "Hollowcreek Lumber Supply",
    "Starling Data Systems",
]
CUSTOMERS = [
    "Alderwood Retail Group",
    "Bayside Medical Partners",
    "Cedar Hollow Foods",
    "Dunmore Hardware Company",
    "Elmstead Construction",
    "Fairhaven Hospitality Group",
    "Glenrock Auto Parts",
    "Hillcrest Dental Associates",
    "Ivywood Apparel",
    "Jadestone Home Goods",
    "Kingsbridge Veterinary Clinic",
    "Lakeside Foods Market",
    "Moonridge Outdoor Supply",
    "Newport Bakery Company",
    "Orchard Lane Pharmacy",
    "Parkview Fitness Studios",
    "Quarry Hill Builders",
    "Rosewood Floral Design",
    "Seabrook Marine Supply",
    "Timberline Sporting Goods",
    "Upland Farm Cooperative",
    "Valleyview Pet Supply",
    "Westbrook Office Interiors",
    "Yellowpine Landscaping",
    "Ashford Garden Center",
    "Briarcliff Bookshop",
    "Coldwater Brewing Company",
]
LANDLORD = "Stonegate Properties"
PAYROLL_PROVIDER = "Evergreen Payroll Services"
ABBREVIATIONS = {
    "Office": "OFC",
    "Supply": "SPLY",
    "Services": "SVCS",
    "Logistics": "LOGIST",
    "Company": "CO",
    "Industrial": "INDL",
    "Manufacturing": "MFG",
    "Solutions": "SOLNS",
    "Systems": "SYS",
    "Group": "GRP",
    "Associates": "ASSOC",
    "Partners": "PTNRS",
    "Distribution": "DIST",
    "Technologies": "TECH",
    "Mechanical": "MECH",
    "Electric": "ELEC",
    "Equipment": "EQUIP",
    "Management": "MGMT",
    "Engineering": "ENGRG",
    "Transport": "TRANSP",
    "Packaging": "PKG",
    "Consulting": "CONSLT",
    "Laboratories": "LABS",
    "Insurance": "INS",
    "Properties": "PROP",
    "Marketing": "MKTG",
    "Janitorial": "JANTRL",
    "Facilities": "FACIL",
    "Construction": "CONSTR",
    "Hospitality": "HOSP",
    "Veterinary": "VET",
    "Medical": "MED",
    "Pharmacy": "PHARM",
    "Translation": "TRANSL",
    "Staffing": "STAFF",
    "Security": "SEC",
}
FEE_DESCRIPTIONS = [
    "MONTHLY SERVICE CHARGE",
    "ACCOUNT ANALYSIS FEE",
    "WIRE TRANSFER FEE",
    "STOP PAYMENT FEE",
    "ACH ORIGINATION FEE",
]
INTEREST_DESCRIPTIONS = ["INTEREST CREDIT", "INTEREST PAID"]


@dataclass
class BankRow:
    key: int
    d: date
    description: str
    amount: Decimal
    check_number: str | None = None
    reference: str | None = None


@dataclass
class GLRow:
    key: int
    d: date
    journal: str
    description: str
    memo: str
    amount: Decimal
    check_number: str | None = None
    reference: str | None = None


@dataclass
class _Event:
    kind: str
    bank: list[BankRow]
    gl: list[GLRow]
    payee: str = ""
    lag_days: int = 0
    tags: set[str] = field(default_factory=set)


@dataclass
class _Break:
    break_type: BreakType
    bank_keys: list[int]
    gl_keys: list[int]
    evidence: str


@dataclass
class GeneratedDataset:
    bank: list[BankRow]
    gl: list[GLRow]
    bank_ids: dict[int, str]
    gl_ids: dict[int, str]
    opening_balance: Decimal
    labels: dict[str, Any]


def parse_month(month: str) -> tuple[int, int]:
    year_s, mon_s = month.split("-")
    year, mon = int(year_s), int(mon_s)
    if not 1 <= mon <= 12:
        raise ValueError(f"invalid month: {month}")
    return year, mon


def parse_break_overrides(spec: str | None) -> dict[BreakType, int]:
    """Parse "OUTSTANDING_CHECK=5,BANK_FEE_UNRECORDED=2" into counts merged over the defaults."""
    counts = dict(DEFAULT_BREAKS)
    if not spec:
        return counts
    for part in spec.split(","):
        if not part.strip():
            continue
        name, _, value = part.partition("=")
        bt = BreakType(name.strip().upper())
        n = int(value)
        if n < 0 or n > 10:
            raise ValueError(f"break count for {bt} must be between 0 and 10")
        counts[bt] = n
    return counts


class _Generator:
    def __init__(self, seed: int, month: str, breaks: dict[BreakType, int]) -> None:
        self.seed = seed
        self.month = month
        self.rng = random.Random(seed)
        self.year, self.mon = parse_month(month)
        last = calendar.monthrange(self.year, self.mon)[1]
        self.start = date(self.year, self.mon, 1)
        self.end = date(self.year, self.mon, last)
        self.bdays = [
            self.start + timedelta(days=i)
            for i in range(last)
            if (self.start + timedelta(days=i)).weekday() < 5
        ]
        self.breaks_plan = breaks
        self._key = 0
        self._used_cents: set[int] = set()
        self._refs: set[str] = set()
        self.events: list[_Event] = []
        self.planted: list[_Break] = []

    # ----- primitives -------------------------------------------------------------------------
    def key(self) -> int:
        self._key += 1
        return self._key

    def amount(self, lo: int, hi: int) -> Decimal:
        """Unique absolute amount in whole dollars range [lo, hi], with random cents."""
        while True:
            cents = self.rng.randint(lo * 100, hi * 100)
            if cents % 100 == 0:
                cents += self.rng.randint(1, 99)
            if cents not in self._used_cents:
                self._used_cents.add(cents)
                return cents_to_money(cents)

    def claim(self, value: Decimal) -> bool:
        cents = abs(money_to_cents(value))
        if cents in self._used_cents or cents == 0:
            return False
        self._used_cents.add(cents)
        return True

    def ref(self, prefix: str, digits: int) -> str:
        while True:
            r = prefix + "".join(str(self.rng.randint(0, 9)) for _ in range(digits))
            if r not in self._refs:
                self._refs.add(r)
                return r

    def noisy(self, name: str) -> str:
        words = []
        for w in name.split():
            if w in ABBREVIATIONS and self.rng.random() < 0.6:
                words.append(ABBREVIATIONS[w])
            else:
                words.append(w.upper())
        text = " ".join(words)
        if self.rng.random() < 0.35:
            text = text[: self.rng.randint(14, 20)].rstrip()
        return text

    def bday_with_lag(
        self, max_lag: int, earliest: int = 0, latest: int | None = None
    ) -> tuple[date, date]:
        """Pick (gl_date, bank_date) business days with 0 <= bank - gl <= max_lag calendar days."""
        latest_idx = len(self.bdays) - 1 if latest is None else latest
        while True:
            i = self.rng.randint(earliest, latest_idx)
            d = self.bdays[i]
            options = [b for b in self.bdays if 0 <= (b - d).days <= max_lag]
            b = self.rng.choice(options)
            return d, b

    def later_bday(self, d: date, min_days: int, max_days: int) -> date | None:
        options = [b for b in self.bdays if min_days <= (b - d).days <= max_days]
        return self.rng.choice(options) if options else None

    # ----- normal activity -------------------------------------------------------------------
    def ach_payment(self, payee: str, lo: int, hi: int, *, lag_ok: bool = True) -> _Event:
        lagged = lag_ok and self.rng.random() < 0.3
        gd, bd = self.bday_with_lag(2 if lagged else 0)
        amt = -self.amount(lo, hi)
        inv = self.ref("INV-", 6)
        trace = self.ref("", 15)
        gl = GLRow(
            self.key(),
            gd,
            "AP",
            f"ACH Payment - {payee}",
            f"{inv} inv amt ${abs(amt):,.2f}",
            amt,
            None,
            trace,
        )
        bank_ref = trace if (not lagged or self.rng.random() < 0.3) else None
        if lagged and bd == gd:
            bank_ref = trace
        bank = BankRow(self.key(), bd, f"ACH DEBIT {self.noisy(payee)}", amt, None, bank_ref)
        return _Event("ach_payment", [bank], [gl], payee, (bd - gd).days)

    def check_payment(self, payee: str) -> _Event:
        gd, bd = self.bday_with_lag(3, latest=len(self.bdays) - 2)
        amt = -self.amount(80, 9500)
        inv = self.ref("INV-", 6)
        gl = GLRow(
            self.key(),
            gd,
            "CD",
            f"Check - {payee}",
            f"{inv} inv amt ${abs(amt):,.2f}",
            amt,
            "",
            None,
        )
        bank = BankRow(self.key(), bd, "", amt, "", None)
        return _Event("check", [bank], [gl], payee, (bd - gd).days)

    def ach_receipt(self, payer: str) -> _Event:
        gd, bd = self.bday_with_lag(1 if self.rng.random() < 0.3 else 0)
        amt = self.amount(100, 15000)
        inv = self.ref("INV-", 6)
        trace = self.ref("", 15)
        gl = GLRow(
            self.key(), gd, "CR", f"ACH receipt - {payer}", f"Payment on {inv}", amt, None, trace
        )
        bank_ref = trace if bd == gd else None
        bank = BankRow(self.key(), bd, f"ACH CREDIT {self.noisy(payer)}", amt, None, bank_ref)
        return _Event("ach_receipt", [bank], [gl], payer, (bd - gd).days)

    def wire_in(self, payer: str) -> _Event:
        d = self.rng.choice(self.bdays)
        amt = self.amount(2000, 60000)
        ref = self.ref("FW", 10)
        gl = GLRow(
            self.key(),
            d,
            "CR",
            f"Wire receipt - {payer}",
            f"Payment on {self.ref('INV-', 6)}",
            amt,
            None,
            ref,
        )
        bank = BankRow(self.key(), d, f"WIRE IN {self.noisy(payer)}", amt, None, ref)
        return _Event("wire_in", [bank], [gl], payer)

    def card_settlement(self, d: date) -> _Event:
        amt = self.amount(500, 9000)
        ref = self.ref("MS", 8)
        gl = GLRow(
            self.key(), d, "CR", "Card settlement batch", f"Processor batch {ref}", amt, None, ref
        )
        bank = BankRow(self.key(), d, f"MERCHANT SETTLEMENT {ref[-4:]}", amt, None, ref)
        return _Event("card_settlement", [bank], [gl], "card processor")

    def payroll(self, d: date, kind: str) -> _Event:
        amt = -self.amount(40000, 90000) if kind == "net" else -self.amount(12000, 30000)
        ref = self.ref("PR", 9)
        desc = "Net payroll" if kind == "net" else "Payroll taxes"
        gl = GLRow(
            self.key(), d, "PR", f"{desc} - {PAYROLL_PROVIDER}", f"Pay run {ref}", amt, None, ref
        )
        bank_desc = "PAYROLL SVC DEBIT EVERGREEN" if kind == "net" else "EVERGREEN PAYROLL TAX"
        bank = BankRow(self.key(), d, bank_desc, amt, None, ref)
        return _Event("payroll", [bank], [gl], PAYROLL_PROVIDER)

    def single_deposit(self, payer: str, latest: int) -> _Event:
        d = self.bdays[self.rng.randint(0, latest)]
        amt = self.amount(300, 12000)
        chk = str(self.rng.randint(1000, 9999))
        ticket = self.ref("DT", 6)
        gl = GLRow(
            self.key(),
            d,
            "CR",
            f"Customer check {chk} - {payer}",
            f"Deposit ticket {ticket}",
            amt,
            None,
            ticket,
        )
        bank = BankRow(self.key(), d, f"DEPOSIT TKT {ticket[2:]}", amt, None, ticket)
        ev = _Event("single_deposit", [bank], [gl], payer)
        ev.tags.add(f"chk:{chk}")
        return ev

    def batch_deposit(self) -> _Event:
        _, bd = self.bday_with_lag(0, earliest=1)
        n = self.rng.randint(2, 5)
        gls: list[GLRow] = []
        for _ in range(n):
            gd = self.rng.choice([b for b in self.bdays if 0 <= (bd - b).days <= 1] or [bd])
            payer = self.rng.choice(CUSTOMERS)
            amt = self.amount(150, 9000)
            chk = str(self.rng.randint(1000, 9999))
            gls.append(
                GLRow(
                    self.key(),
                    gd,
                    "CR",
                    f"Customer check {chk} - {payer}",
                    "Remote deposit capture",
                    amt,
                    None,
                    None,
                )
            )
        total = to_money(sum((g.amount for g in gls), ZERO))
        if not self.claim(total):  # astronomically rare; keep totals unique anyway
            return self.batch_deposit()
        desc = self.rng.choice(["REMOTE DEPOSIT", "BRANCH DEPOSIT", "MOBILE DEPOSIT"])
        bank = BankRow(self.key(), bd, desc, total, None, None)
        return _Event("batch_deposit", [bank], gls, "multiple customers")

    def build_activity(self) -> None:
        r = self.rng
        for _ in range(158):
            self.events.append(self.ach_payment(r.choice(VENDORS), 150, 18000))
        for _ in range(80):
            self.events.append(self.check_payment(r.choice(VENDORS)))
        for _ in range(70):
            self.events.append(self.ach_receipt(r.choice(CUSTOMERS)))
        for _ in range(30):
            self.events.append(self.wire_in(r.choice(CUSTOMERS)))
        for d in self.bdays:
            self.events.append(self.card_settlement(d))
        pay_days = [
            self.bdays[min(6, len(self.bdays) - 1)],
            self.bdays[min(16, len(self.bdays) - 1)],
        ]
        for d in pay_days:
            self.events.append(self.payroll(d, "net"))
            self.events.append(self.payroll(d, "tax"))
        rent = self.ach_payment(LANDLORD, 9000, 15000, lag_ok=False)
        rent.gl[0].d = rent.bank[0].d = self.bdays[0]
        self.events.append(rent)
        n_nsf = self.breaks_plan.get(BreakType.NSF_RETURN_UNRECORDED, 0)
        for _ in range(max(8, n_nsf + 4)):
            self.events.append(self.single_deposit(r.choice(CUSTOMERS), len(self.bdays) - 6))
        for _ in range(12):
            self.events.append(self.batch_deposit())

    # ----- planted breaks --------------------------------------------------------------------
    def pick(self, kind: str, pred: Any = None) -> _Event:
        pool = [
            e
            for e in self.events
            if e.kind == kind and "planted" not in e.tags and (pred is None or pred(e))
        ]
        if not pool:
            raise RuntimeError(f"no host transaction available for planting on {kind}")
        ev = self.rng.choice(pool)
        ev.tags.add("planted")
        return ev

    def plant(self) -> None:
        plan = self.breaks_plan
        r = self.rng
        for _ in range(plan.get(BreakType.DEPOSIT_IN_TRANSIT, 0)):
            d = r.choice(self.bdays[-2:])
            payer = r.choice(CUSTOMERS)
            ticket = self.ref("DT", 6)
            gl = GLRow(
                self.key(),
                d,
                "CR",
                f"Deposit - {payer}",
                f"Deposit ticket {ticket} taken to bank after cutoff",
                self.amount(1000, 25000),
                None,
                ticket,
            )
            self.events.append(_Event("dit", [], [gl], payer, tags={"planted"}))
            self.planted.append(
                _Break(
                    BreakType.DEPOSIT_IN_TRANSIT,
                    [],
                    [gl.key],
                    "GL receipt dated at period end with no bank deposit",
                )
            )
        for _ in range(plan.get(BreakType.OUTSTANDING_CHECK, 0)):
            d = r.choice(self.bdays[-6:])
            payee = r.choice(VENDORS)
            amt = -self.amount(200, 12000)
            gl = GLRow(
                self.key(),
                d,
                "CD",
                f"Check - {payee}",
                f"{self.ref('INV-', 6)} inv amt ${abs(amt):,.2f}",
                amt,
                "",
                None,
            )
            self.events.append(_Event("check_os", [], [gl], payee, tags={"planted"}))
            self.planted.append(
                _Break(
                    BreakType.OUTSTANDING_CHECK,
                    [],
                    [gl.key],
                    "Check in our series issued late in month, not cleared",
                )
            )
        for _ in range(plan.get(BreakType.BANK_FEE_UNRECORDED, 0)):
            d = r.choice(self.bdays)
            bank = BankRow(self.key(), d, r.choice(FEE_DESCRIPTIONS), -self.amount(12, 350))
            self.events.append(_Event("fee", [bank], [], "bank", tags={"planted"}))
            self.planted.append(
                _Break(
                    BreakType.BANK_FEE_UNRECORDED, [bank.key], [], "Bank service charge description"
                )
            )
        for _ in range(plan.get(BreakType.INTEREST_UNRECORDED, 0)):
            bank = BankRow(
                self.key(), self.bdays[-1], r.choice(INTEREST_DESCRIPTIONS), self.amount(15, 900)
            )
            self.events.append(_Event("interest", [bank], [], "bank", tags={"planted"}))
            self.planted.append(
                _Break(
                    BreakType.INTEREST_UNRECORDED,
                    [bank.key],
                    [],
                    "Bank interest credit description",
                )
            )
        for _ in range(plan.get(BreakType.NSF_RETURN_UNRECORDED, 0)):
            host = self.pick("single_deposit")
            dep = host.gl[0]
            chk = next(t for t in host.tags if t.startswith("chk:"))[4:]
            rd = self.later_bday(dep.d, 2, 9) or self.bdays[-1]
            bank = BankRow(
                self.key(),
                rd,
                f"RETURNED ITEM NSF CHK {chk} {self.noisy(host.payee)}",
                -dep.amount,
                None,
                self.ref("RTN", 8),
            )
            self.events.append(_Event("nsf", [bank], [], host.payee, tags={"planted"}))
            self.planted.append(
                _Break(
                    BreakType.NSF_RETURN_UNRECORDED,
                    [bank.key],
                    [],
                    f"NSF return cites customer check {chk} deposited earlier",
                )
            )
        for _ in range(plan.get(BreakType.GL_DUPLICATE, 0)):
            host = self.pick(
                "ach_payment",
                lambda e: e.lag_days == 0 and e.bank[0].reference and e.gl[0].d < self.bdays[-1],
            )
            orig = host.gl[0]
            dd = self.later_bday(orig.d, 1, 4)
            if dd is None:  # pragma: no cover - a later business day always exists for hosts
                raise RuntimeError("no later business day for duplicate posting")
            dup = GLRow(
                self.key(),
                dd,
                orig.journal,
                orig.description,
                orig.memo,
                orig.amount,
                orig.check_number,
                orig.reference,
            )
            self.events.append(_Event("dup", [], [dup], host.payee, tags={"planted"}))
            self.planted.append(
                _Break(
                    BreakType.GL_DUPLICATE,
                    [],
                    [dup.key],
                    "Same invoice and ACH trace posted twice in the GL",
                )
            )
        for _ in range(plan.get(BreakType.GL_TRANSPOSITION, 0)):
            host = self.pick(r.choice(["ach_payment", "check"]))
            g, b = host.gl[0], host.bank[0]
            g.amount = self.transpose(b.amount)
            host.tags.add("transposed")
            self.planted.append(
                _Break(
                    BreakType.GL_TRANSPOSITION,
                    [b.key],
                    [g.key],
                    "GL amount is the bank amount with two digits swapped",
                )
            )
        for _ in range(plan.get(BreakType.GL_WRONG_AMOUNT, 0)):
            host = self.pick("ach_payment")
            g, b = host.gl[0], host.bank[0]
            g.amount = self.wrong_amount(b.amount)
            self.planted.append(
                _Break(
                    BreakType.GL_WRONG_AMOUNT,
                    [b.key],
                    [g.key],
                    "GL memo invoice amount equals bank amount; booked differs",
                )
            )
        for _ in range(plan.get(BreakType.BANK_ERROR, 0)):
            d = r.choice(self.bdays)
            notice = self.ref("ADJN", 6)
            if r.random() < 0.5:
                serial = str(r.randint(70000, 79999))
                bank = BankRow(
                    self.key(), d, f"CHECK {serial}", -self.amount(200, 6000), serial, notice
                )
            else:
                bank = BankRow(self.key(), d, "DEPOSIT", self.amount(200, 6000), None, notice)
            self.events.append(_Event("bank_error", [bank], [], "bank", tags={"planted"}))
            self.planted.append(
                _Break(
                    BreakType.BANK_ERROR,
                    [bank.key],
                    [],
                    f"Bank adjustment notice {notice}: item posted in error",
                )
            )

    def transpose(self, amount: Decimal) -> Decimal:
        sign = -1 if amount < 0 else 1
        digits = str(abs(money_to_cents(amount)))
        positions = [i for i in range(len(digits) - 3) if digits[i] != digits[i + 1]]
        self.rng.shuffle(positions)
        for i in positions:
            if i == 0 and digits[1] == "0":
                continue
            swapped = digits[:i] + digits[i + 1] + digits[i] + digits[i + 2 :]
            candidate = cents_to_money(sign * int(swapped))
            if self.claim(candidate):
                return candidate
        raise RuntimeError("could not build a unique transposition")

    def wrong_amount(self, amount: Decimal) -> Decimal:
        sign = -1 if amount < 0 else 1
        true_cents = abs(money_to_cents(amount))
        while True:
            delta = self.rng.randint(1000, 50000) * self.rng.choice([-1, 1])
            cents = true_cents + delta
            if cents <= 0 or is_digit_swap(true_cents, cents):
                continue
            candidate = cents_to_money(sign * cents)
            if self.claim(candidate):
                return candidate

    # ----- assembly ----------------------------------------------------------------------------
    def assign_check_numbers(self) -> None:
        checks = [e for e in self.events if e.kind in ("check", "check_os")]
        checks.sort(key=lambda e: (e.gl[0].d, e.gl[0].key))
        start = self.rng.randint(10200, 10800)
        for offset, ev in enumerate(checks):
            no = str(start + offset)
            ev.gl[0].check_number = no
            ev.gl[0].description = f"Check {no} - {ev.payee}"
            if ev.bank:
                ev.bank[0].check_number = no
                ev.bank[0].description = self.rng.choice(
                    [f"CHECK {no}", f"CHK #{no}", f"CHECK PAID {no}"]
                )

    def build(self) -> GeneratedDataset:
        self.build_activity()
        self.plant()
        self.assign_check_numbers()
        bank = [b for e in self.events for b in e.bank]
        gl = [g for e in self.events for g in e.gl]
        bank_order = {b.key: self.rng.randrange(10**9) for b in bank}
        gl_order = {g.key: self.rng.randrange(10**9) for g in gl}
        bank.sort(key=lambda b: (b.d, bank_order[b.key]))
        gl.sort(key=lambda g: (g.d, gl_order[g.key]))
        bank_ids = {b.key: f"BNK-{i:05d}" for i, b in enumerate(bank, start=1)}
        gl_ids = {g.key: f"GL-{i:05d}" for i, g in enumerate(gl, start=1)}
        opening = self.amount(500000, 900000)
        labels = self.labels(bank, gl, bank_ids, gl_ids, opening)
        return GeneratedDataset(bank, gl, bank_ids, gl_ids, opening, labels)

    def labels(
        self,
        bank: list[BankRow],
        gl: list[GLRow],
        bank_ids: dict[int, str],
        gl_ids: dict[int, str],
        opening: Decimal,
    ) -> dict[str, Any]:
        bank_by_key = {b.key: b for b in bank}
        gl_by_key = {g.key: g for g in gl}
        bank_end = to_money(opening + sum((b.amount for b in bank), ZERO))
        gl_end = to_money(opening + sum((g.amount for g in gl), ZERO))
        transposed_or_wrong = {
            k
            for br in self.planted
            if br.break_type in (BreakType.GL_TRANSPOSITION, BreakType.GL_WRONG_AMOUNT)
            for k in br.bank_keys
        }
        matches = []
        for ev in self.events:
            if not ev.bank or not ev.gl or ev.bank[0].key in transposed_or_wrong:
                continue
            matches.append(
                {
                    "kind": ev.kind,
                    "bank_ids": sorted(bank_ids[b.key] for b in ev.bank),
                    "gl_ids": sorted(gl_ids[g.key] for g in ev.gl),
                }
            )
        matches.sort(key=lambda m: (m["bank_ids"], m["gl_ids"]))
        breaks = []
        bank_adj = ZERO
        book_adj = ZERO
        for br in self.planted:
            bt = br.break_type
            b_amt = sum((bank_by_key[k].amount for k in br.bank_keys), ZERO)
            g_amt = sum((gl_by_key[k].amount for k in br.gl_keys), ZERO)
            if bt in (BreakType.DEPOSIT_IN_TRANSIT, BreakType.OUTSTANDING_CHECK):
                side, adj = "BANK", g_amt
            elif bt == BreakType.BANK_ERROR:
                side, adj = "BANK", -b_amt
            elif bt in (
                BreakType.BANK_FEE_UNRECORDED,
                BreakType.INTEREST_UNRECORDED,
                BreakType.NSF_RETURN_UNRECORDED,
            ):
                side, adj = "BOOK", b_amt
            elif bt == BreakType.GL_DUPLICATE:
                side, adj = "BOOK", -g_amt
            else:
                side, adj = "BOOK", b_amt - g_amt
            if side == "BANK":
                bank_adj += adj
            else:
                book_adj += adj
            breaks.append(
                {
                    "break_type": bt.value,
                    "bank_ids": [bank_ids[k] for k in br.bank_keys],
                    "gl_ids": [gl_ids[k] for k in br.gl_keys],
                    "adjusts": side,
                    "adjustment": format(to_money(adj), "f"),
                    "evidence": br.evidence,
                }
            )
        breaks.sort(key=lambda x: (x["break_type"], x["bank_ids"], x["gl_ids"]))
        adjusted_bank = to_money(bank_end + bank_adj)
        adjusted_book = to_money(gl_end + book_adj)
        if adjusted_bank != adjusted_book:  # pragma: no cover - generator invariant
            raise AssertionError("generator produced an inconsistent dataset")
        return {
            "seed": self.seed,
            "period": self.month,
            "opening_balance": format(opening, "f"),
            "bank_ending_balance": format(bank_end, "f"),
            "gl_ending_balance": format(gl_end, "f"),
            "expected_adjusted_balance": format(adjusted_bank, "f"),
            "break_counts": {bt.value: n for bt, n in sorted(self.breaks_plan.items())},
            "matches": matches,
            "breaks": breaks,
        }


def is_digit_swap(a_cents: int, b_cents: int) -> bool:
    """True when b is a with exactly two digits swapped (same length)."""
    a, b = str(abs(a_cents)), str(abs(b_cents))
    if len(a) != len(b) or a == b:
        return False
    diff = [i for i in range(len(a)) if a[i] != b[i]]
    return len(diff) == 2 and a[diff[0]] == b[diff[1]] and a[diff[1]] == b[diff[0]]


def generate(seed: int, month: str, breaks: dict[BreakType, int] | None = None) -> GeneratedDataset:
    return _Generator(seed, month, breaks or dict(DEFAULT_BREAKS)).build()


def write_dataset(ds: GeneratedDataset, out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    bank_path = out_dir / "bank_statement.csv"
    gl_path = out_dir / "gl_cash.csv"
    labels_path = out_dir / "labels.json"
    balance = ds.opening_balance
    with bank_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(
            ["txn_id", "date", "description", "amount", "check_number", "reference", "balance"]
        )
        for b in ds.bank:
            balance = to_money(balance + b.amount)
            w.writerow(
                [
                    ds.bank_ids[b.key],
                    b.d.isoformat(),
                    b.description,
                    format(b.amount, "f"),
                    b.check_number or "",
                    b.reference or "",
                    format(balance, "f"),
                ]
            )
    balance = ds.opening_balance
    with gl_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(
            [
                "entry_id",
                "date",
                "journal",
                "description",
                "memo",
                "reference",
                "check_number",
                "debit",
                "credit",
                "balance",
            ]
        )
        for g in ds.gl:
            balance = to_money(balance + g.amount)
            debit = format(g.amount, "f") if g.amount > 0 else ""
            credit = format(-g.amount, "f") if g.amount < 0 else ""
            w.writerow(
                [
                    ds.gl_ids[g.key],
                    g.d.isoformat(),
                    g.journal,
                    g.description,
                    g.memo,
                    g.reference or "",
                    g.check_number or "",
                    debit,
                    credit,
                    format(balance, "f"),
                ]
            )
    labels_path.write_text(json.dumps(ds.labels, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"bank": bank_path, "gl": gl_path, "labels": labels_path}
