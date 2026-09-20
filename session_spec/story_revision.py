import json
from pathlib import Path

from .storage import file_hash


def load_revision(directory, packet, source):
    support = Path(directory).expanduser().resolve() / "_support"
    work = support / ".work"
    locations = ((work, "edition-candidate.json", "edition-attempt.json", "candidate_sha256", "unapproved_candidate"),
                 (support, "edition.json", "edition-receipt.json", "output_sha256", "previous_edition"))
    for root, name, receipt_name, hash_key, kind in locations:
        draft_path, receipt_path = root / name, root / receipt_name
        if not draft_path.is_file() or not receipt_path.is_file():
            continue
        metadata = json.loads((root / "source.json").read_bytes())
        if metadata.get("source_sha256") != source["source_sha256"] or json.loads((root / "input.json").read_bytes()) != packet:
            raise ValueError("Revision seed does not match the selected source snapshot and interpreted events")
        receipt = json.loads(receipt_path.read_bytes())
        if receipt.get(hash_key) != file_hash(draft_path):
            raise ValueError("Revision seed differs from its recorded candidate/edition hash")
        return json.loads(draft_path.read_bytes()), {"method": "source_checked_revision", "kind": kind,
                "seed_sha256": file_hash(draft_path), "source_sha256": source["source_sha256"],
                "note": "Starting material only. Approval is not inherited; the current whole-document contract reviews original events again."}
    raise ValueError("Revision seed needs a hash-bound prior edition or saved edition candidate")
