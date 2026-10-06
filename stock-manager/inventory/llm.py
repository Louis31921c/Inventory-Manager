import asyncio
import base64
import json
import os
import shutil
import tempfile
from datetime import date
from pathlib import Path

from .models import Note

_STR = {"anyOf": [{"type": "string"}, {"type": "null"}]}
_NUM = {"anyOf": [{"type": "number"}, {"type": "null"}]}

HERE = Path(__file__).parent
RULES_FILE = HERE / "rules.md"

PRIVATE_RULES_FILE = Path(os.environ.get("DATA_DIR", "./data")) / "private_rules.md"


def prompt_text(name):
   
    return (HERE / "prompts" / name).read_text(encoding="utf-8").strip()


class ExtractionError(Exception):
    pass


EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "supplier": _STR,
        "delivery_date": _STR,
        "order_date": _STR,
        "site": _STR,
        "work_item": _STR,
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "article": {"type": "string"},
                    "quantity": _NUM,
                    "backorder": {"type": "boolean"},
                    "designation": {"type": "string"},
                },
                "required": ["article", "quantity", "backorder", "designation"],
                "additionalProperties": False,
            },
        },
        "warnings": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["supplier", "delivery_date", "order_date", "site", "work_item", "lines",
                 "warnings"],
    "additionalProperties": False,
}

LIST_SCHEMA = {
    "type": "object",
    "properties": {
        "list_date": _STR,
        "site": _STR,
        "work_item": _STR,
        "drafter": _STR,
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "article": {"type": "string"},
                    "designation": {"type": "string"},
                    "reference": _STR,
                    "quantity": _NUM,
                    "stock": _NUM,
                },
                "required": ["article", "designation", "reference", "quantity", "stock"],
                "additionalProperties": False,
            },
        },
        "warnings": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["list_date", "site", "work_item", "drafter", "lines", "warnings"],
    "additionalProperties": False,
}

SQL_SCHEMA = {
    "type": "object",
    "properties": {"sql": _STR, "note": _STR},
    "required": ["sql", "note"],
    "additionalProperties": False,
}


def _house_rules():

    rules = []
    for path in (RULES_FILE, PRIVATE_RULES_FILE):
        try:
            rules.append(path.read_text(encoding="utf-8").strip())
        except FileNotFoundError:
            pass
    if not rules:
        return ""
    return "\n\n<house_rules>\n" + "\n\n".join(rules) + "\n</house_rules>"


def _vocabulary(known_articles):
    names = "\n".join(f"- {a}" for a in known_articles or ())
    if not names:
        return ""
    return (
        "\n\n<known_articles>\nArticle names already used in our database, the shared vocabulary "
        "built from both delivery notes and purchase orders, most frequent first. When a line is the "
        "same product (same type and same defining size or colour), reuse the exact name from this "
        "list, even if the document words it differently. Only create a new name for a product that "
        f"is not in the list.\n{names}\n</known_articles>"
    )


def extraction_prompt(known_articles=()):
    return prompt_text("delivery_note.md") + _house_rules() + _vocabulary(known_articles)


def list_prompt(known_articles=()):
    
    return prompt_text("purchase_order.md") + _house_rules() + _vocabulary(known_articles)


def _question_prompt(question, previous):
    prompt = f"Today is {date.today().isoformat()}.\n\nQuestion: {question}"
    if previous:
        bad_sql, error = previous
        prompt += (f"\n\nYour previous query:\n{bad_sql}\nfailed with:\n{error}\n\n"
                   f"Return a corrected query.")
    return prompt


def _to_note(result):
    try:
        return Note.model_validate(result)
    except ValueError as e:
        raise ExtractionError(f"Unreadable answer from the model: {e}") from e


class ClaudeCodeBackend:
    

    SUFFIXES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
                "image/gif": ".gif", "application/pdf": ".pdf"}

    def __init__(self, model=None, binary="claude", timeout=300):
        self.model = model
        self.binary = shutil.which(binary) or binary
        self.timeout = timeout

    async def extract(self, data, media_type, known_articles=()):
        result = await self._read_file(data, media_type, "note", EXTRACT_SCHEMA,
                                       "a photo or scan of a delivery note",
                                       extraction_prompt(known_articles))
        return _to_note(result)

    async def extract_list(self, data, media_type, known_articles=()):
        return await self._read_file(data, media_type, "list", LIST_SCHEMA,
                                     "a photo of a purchase order sheet",
                                     list_prompt(known_articles))

    async def to_sql(self, question, previous=None):
        with tempfile.TemporaryDirectory(prefix="sql-") as tmp:
            result = await self._run(_question_prompt(question, previous), SQL_SCHEMA,
                                     cwd=tmp, system=prompt_text("sql.md"))
        return result.get("sql"), result.get("note")

    async def _read_file(self, data, media_type, stem, schema, what, instructions):
        with tempfile.TemporaryDirectory(prefix=f"{stem}-") as tmp:
            name = stem + self.SUFFIXES.get(media_type, ".jpg")
            (Path(tmp) / name).write_bytes(data)
            prompt = f"Read the file ./{name} ({what}), then answer.\n\n{instructions}"
            return await self._run(prompt, schema, cwd=tmp, tools="Read")

    async def _run(self, prompt, schema, cwd, tools="", system=None):
        args = [self.binary, "-p", "--output-format", "json", "--json-schema", json.dumps(schema),
                "--no-session-persistence", "--tools", tools]
        if tools:
            args += ["--allowedTools", tools]
        if system:
            args += ["--system-prompt", system]
        if self.model:
            args += ["--model", self.model]

        try:
            proc = await asyncio.create_subprocess_exec(
                *args, cwd=cwd, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as e:
            raise ExtractionError(f"Claude CLI not found ({self.binary}).") from e

        try:
            out, err = await asyncio.wait_for(proc.communicate(prompt.encode()), self.timeout)
        except asyncio.TimeoutError as e:
            proc.kill()
            raise ExtractionError(f"Claude did not answer within {self.timeout:.0f} s.") from e

        try:
            data = json.loads(out)
        except json.JSONDecodeError as e:
            detail = (err or out).decode(errors="replace").strip()[:300]
            raise ExtractionError(f"Claude CLI failed: {detail}") from e

        if data.get("is_error") or data.get("subtype") != "success":
            detail = str(data.get("result") or data.get("subtype"))[:300]
            if "limit" in detail.lower():
                raise ExtractionError(f"Claude usage limit reached: {detail}")
            raise ExtractionError(f"Claude failed: {detail}")
        if not isinstance(data.get("structured_output"), dict):
            raise ExtractionError("Claude returned no structured data.")
        return data["structured_output"]


class GeminiBackend:
    def __init__(self, api_key, model="gemini-3.8-flash"):
        from google import genai  

        self.client = genai.Client(api_key=api_key)
        self.model = model

    async def extract(self, data, media_type, known_articles=()):
        result = await self._call(
            input=[self._file(data, media_type),
                   {"type": "text", "text": extraction_prompt(known_articles)}],
            response_format=_json_format(EXTRACT_SCHEMA),
        )
        return _to_note(result)

    async def extract_list(self, data, media_type, known_articles=()):
        return await self._call(
            input=[self._file(data, media_type),
                   {"type": "text", "text": list_prompt(known_articles)}],
            response_format=_json_format(LIST_SCHEMA),
        )

    async def to_sql(self, question, previous=None):
        result = await self._call(
            system_instruction=prompt_text("sql.md"),
            input=_question_prompt(question, previous),
            response_format=_json_format(SQL_SCHEMA),
        )
        return result.get("sql"), result.get("note")

    @staticmethod
    def _file(data, media_type):
        kind = "document" if media_type == "application/pdf" else "image"
        return {"type": kind, "data": base64.b64encode(data).decode(), "mime_type": media_type}

    async def _call(self, **kwargs):
        try:
            interaction = await self.client.aio.interactions.create(
                model=self.model, store=False, **kwargs)
        except Exception as e: 
            msg = str(e)
            if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
                raise ExtractionError(
                    "Gemini free quota reached, try again in a minute (or tomorrow).") from e
            raise ExtractionError(f"Gemini API error: {msg[:300]}") from e

        if interaction.status != "completed":
            raise ExtractionError(
                f"Incomplete answer from the model (status: {interaction.status}).")
        try:
            return json.loads(interaction.output_text or "")
        except json.JSONDecodeError as e:
            raise ExtractionError(f"Unreadable answer from the model: {e}") from e


def _json_format(schema):
    return {"type": "text", "mime_type": "application/json", "schema": schema}


def make_backend(config):
    if config.backend == "gemini":
        return GeminiBackend(config.gemini_api_key, config.model or "gemini-3.8-flash")
    return ClaudeCodeBackend(config.model or None)
