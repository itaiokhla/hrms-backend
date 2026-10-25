"""Summarize the payroll E2E stack's development e-mail outbox for the Chrome suite (read-only).

    python scripts/e2e_payroll_outbox.py <company_id> [template-prefix]

The stack (scripts/payroll_stack.py) writes every rendered e-mail to ``logs/outbox-payroll/<company_id>/``
instead of sending it. Prints a JSON list with, per e-mail: the template key, the sender's display name, the
subject, the plain-text body, the ``#RRGGBB`` colours used in the HTML part and whether an image is embedded.
Never prints an e-mail address (the From/To addresses are dropped; addresses in bodies are replaced by
``<address>``), so the output can be summarised into evidence.
"""

import email
import json
import re
import sys
from email import policy
from email.utils import parseaddr
from pathlib import Path

OUTBOX = Path(__file__).resolve().parents[1] / "logs" / "outbox-payroll"
ADDRESS = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def summarize(path: Path) -> dict:
    message = email.message_from_bytes(path.read_bytes(), policy=policy.default)
    plain = message.get_body(preferencelist=("plain",))
    html = message.get_body(preferencelist=("html",))
    html_text = html.get_content() if html is not None else ""
    return {
        "template": path.name.split("-", 1)[0],
        "file_id": path.stem.rsplit("-", 1)[-1],
        "from_name": parseaddr(str(message.get("From", "")))[0],
        "subject": str(message.get("Subject", "")),
        "text": ADDRESS.sub("<address>", plain.get_content() if plain is not None else ""),
        "html_colours": sorted({c.upper() for c in re.findall(r"#[0-9A-Fa-f]{6}\b", html_text)}),
        "html_has_image": "<img" in html_text.lower(),
    }


def main(argv) -> None:
    company_id = int(argv[0])
    prefix = argv[1] if len(argv) > 1 else ""
    folder = OUTBOX / str(company_id)
    rows = [summarize(p) for p in sorted(folder.glob("*.eml")) if p.name.startswith(prefix)] if folder.exists() else []
    sys.stdout.write(json.dumps(rows, ensure_ascii=False))


if __name__ == "__main__":
    main(sys.argv[1:])
