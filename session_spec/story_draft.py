import json
import copy

from .pipeline import write_json
from .storage import PROMPTS, digest, file_hash
from .story_context import story_context
from .story_grounding import complete_reference_pairs
from .language import language_contract
from .model_io import generate_json


def normalize_containers(draft):
    if (isinstance(draft, dict) and set(draft) == {"article"} and isinstance(draft["article"], dict)
            and isinstance(draft["article"].get("brief"), dict) and isinstance(draft["article"].get("insights"), dict)
            and draft["article"]["brief"].get("schema") == "story-brief/v1"
            and draft["article"]["insights"].get("schema") == "story-insights/v1"):
        result = copy.deepcopy(draft)
        result["brief"] = result["article"].pop("brief")
        result["insights"] = result["article"].pop("insights")
        return result, ["Moved complete brief/insights from article to their required root containers; no field content changed"]
    return draft, []


def generate_draft(directory, packet, canonical, backend, model=None, language="auto"):
    contract = (PROMPTS / "story-draft.md").read_text(encoding="utf-8")
    contract += "\n\n" + (PROMPTS / "agent-detail.md").read_text(encoding="utf-8") + language_contract(language)
    editorial = (PROMPTS / "story-editor.md").read_text(encoding="utf-8")
    context = story_context(packet, canonical)
    prompt = contract + "\n\n发布前将按以下约定复核。这里只把它当写作约束，不执行其审阅输出任务：\n" + editorial
    prompt += context + "\n\n现在只返回完整初稿 JSON，顶层严格为 article、brief、insights，不返回 issues/checked/summary 或 patches。"
    prompt += "输出前检查 /brief/schema=story-brief/v1、/insights/schema=story-insights/v1、/article/agent_detail 的完整 agent-detail/v3 结构、每章真实 refs，以及 /article/agent_markdown 工作约定与机制中的行内来源；这些均不可省略。"
    identity = digest(("joint-story-draft/v2" + prompt + (model or "copilot-default")).encode())
    output, receipt_path = directory / "joint-draft.json", directory / "joint-draft-receipt.json"
    if output.is_file() and receipt_path.is_file():
        receipt = json.loads(receipt_path.read_bytes())
        if receipt.get("identity") == identity and receipt.get("output_sha256") == file_hash(output):
            print("[draft cached] shared human and Agent edition", flush=True)
            draft, relocations = normalize_containers(json.loads(output.read_bytes()))
            if relocations:
                write_json(directory / "joint-container-relocations.json", relocations)
                write_json(output, draft)
                write_json(receipt_path, {**receipt, "output_sha256": file_hash(output), "container_relocations": relocations})
            return draft
    raw_draft = generate_json(backend, prompt, "story-joint-draft", directory)
    write_json(directory / "joint-raw-draft.json", raw_draft)
    draft, relocations = normalize_containers(raw_draft)
    write_json(directory / "joint-container-relocations.json", relocations)
    draft, additions = complete_reference_pairs(draft, packet)
    write_json(directory / "joint-reference-pairs.json", additions)
    write_json(output, draft)
    write_json(receipt_path, {"identity": identity, "output_sha256": file_hash(output), "status": "draft_not_approved"})
    return draft
