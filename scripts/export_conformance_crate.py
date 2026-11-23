"""Export the fixture model as a `.eln` crate for the conformance check. Needs no server.

Stage one of the two-stage conformance job: this runs in the project environment (Python
3.14) and writes a crate; `check_crate_conformance.py` validates it in a separate 3.13
environment, because rocrate-validator's dependencies do not build on 3.14.

usage: export_conformance_crate.py [--withheld] <output.eln>

The fixture has two collections with one sample in both, elements of every kind, a reaction
with an analysis and a real attachment. ``--withheld`` exports with ``disclose_host=False``.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from chemotion_crate import Author, AuthorAffiliation, export_model  # noqa: E402
from chemotion_crate.model import ExportModel  # noqa: E402

FIXTURE = REPO / "tests" / "fixtures" / "mini_model.json"
AUTHORS = [Author(name="Conformance Fixture", orcid="0000-0002-1825-0097",
                  affiliations=[AuthorAffiliation(name="Example University",
                                                  ror="https://ror.org/05dxps055")],
                  email="no-reply@eln.invalid")]


def main(out_path: str, withheld: bool = False) -> int:
    model = ExportModel.load(FIXTURE)
    # No network in CI; the embedded CHMO codes are what we want to exercise anyway.
    export_model(model, out_path, authors=AUTHORS, ontology_lookup=False,
                 disclose_host=not withheld)
    written = Path(out_path)
    if not written.is_file() or written.stat().st_size == 0:
        print(f"ERROR: no crate written to {out_path}", file=sys.stderr)
        return 1
    print(f"exported {out_path} ({written.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    withheld = "--withheld" in args
    args = [a for a in args if a != "--withheld"]
    if len(args) != 1:
        print(__doc__, file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(main(args[0], withheld=withheld))
