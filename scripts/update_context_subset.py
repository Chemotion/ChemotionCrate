"""Regenerate chemotion_crate/data/ro-crate-context.jsonld from the official RO-Crate 1.2 context.

usage: update_context_subset.py [<path-or-url of the official context>]

Keeps every prefix declaration of the official context and the terms listed in
``chemotion_crate.context_terms.CONTEXT_TERMS``, each copied verbatim. Fetches
https://w3id.org/ro/crate/1.2/context when no source is given.
"""
import json
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from chemotion_crate.context_terms import CONTEXT_TERMS  # noqa: E402
from chemotion_crate.graph import RO_CRATE_CONTEXT_URL  # noqa: E402
from chemotion_crate.namespaces import prefixes_of_context  # noqa: E402

TARGET = REPO / "chemotion_crate" / "data" / "ro-crate-context.jsonld"


def load(source: str | None) -> dict:
    if source and not source.startswith(("http://", "https://")):
        return json.loads(Path(source).read_text(encoding="utf-8"))
    with urllib.request.urlopen(source or RO_CRATE_CONTEXT_URL, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def main(source: str | None) -> int:
    full = load(source)["@context"]
    missing = sorted(t for t in CONTEXT_TERMS if t not in full)
    if missing:
        print(f"ERROR: not in the official context: {missing}", file=sys.stderr)
        return 1
    keep = set(CONTEXT_TERMS) | set(prefixes_of_context(full)) | {k for k in full if k.startswith("@")}
    subset = {term: full[term] for term in full if term in keep}  # official order
    TARGET.write_text(json.dumps({
        "_comment": f"Subset of {RO_CRATE_CONTEXT_URL}: its prefix declarations plus the terms in "
                    "chemotion_crate.context_terms, copied verbatim. Regenerate with "
                    "scripts/update_context_subset.py.",
        "@context": subset,
    }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {TARGET}: {len(subset)} entries, {TARGET.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else None))
