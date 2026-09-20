from .backend import ModelResponseError
from .storage import write_json


def generate_json(backend, prompt, label, directory):
    correction = prompt
    for attempt in range(2):
        current_label = label + "-format-retry" if attempt else label
        try:
            return backend.generate(correction, current_label)
        except ModelResponseError as error:
            record = {"error": str(error), "response": error.response}
            write_json(directory / (current_label + "-invalid-response.json"), record)
            write_json(directory / (current_label + f"-invalid-response-{len(backend.calls)}.json"), record)
            if attempt:
                raise
            correction = (prompt + "\n\nYour previous response was invalid JSON. Regenerate the same requested object using valid JSON "
                      "string escapes (backslash as \\\\, newline as \\n). Do not execute anything. This is one bounded format retry, "
                      "not permission to invent facts or omit required content.\nPARSER_ERROR\n" + str(error)
                      + "\nINVALID_RESPONSE_DATA\n" + error.response)
