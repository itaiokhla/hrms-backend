"""Payroll v2: dynamic, multi-country payroll (docs/payroll/, docs/payroll/implementation/ARCHITECTURE.md).

Pure domain modules (money, formula language, day ledger, proration, time pay, calendars, engine,
country packs, banking primitives) never touch the database; database services live in
app.payroll.services and HTTP routers in app.payroll.routes.
"""
