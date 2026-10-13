"""Heading-aware deterministic paragraph packing, without an LLM or tiny fragments."""

import re
from hashlib import sha256
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5
from enterprise_ai_ops_agent.knowledge.contracts import KnowledgeChunk, KnowledgeError, MAX_CHUNK_CHARS

DISCLAIMER = "Demo internal policy for the Enterprise AI Operations Agent portfolio project."


def chunk_markdown(text: str, *, document_id: str, source_name: str) -> list[KnowledgeChunk]:
    lines=text.replace("\r\n", "\n").replace("\r", "\n").strip().splitlines()
    if not lines or not lines[0].startswith("# ") or not lines[0][2:].strip():
        raise KnowledgeError("Markdown documents require a nonblank first-line H1 title.")
    title=lines[0][2:].strip()
    if len(title)>150: raise KnowledgeError("Knowledge title exceeds its supported length.")
    sections=[]; headings=[]; body=[]; current="Introduction"; fence=None
    for line in lines[1:]:
        marker=re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            kind=marker[1][0]
            fence=None if fence==kind else (kind if fence is None else fence)
        heading=re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line) if fence is None else None
        if heading:
            if len(heading[1])==1: raise KnowledgeError("Only one document H1 title is supported.")
            if body and "\n".join(body).strip(): sections.append((current,"\n".join(body).strip()))
            body=[]; depth=len(heading[1])
            headings=[item for item in headings if item[0]<depth]+[(depth,heading[2])]
            current=" / ".join(item[1] for item in headings)
            if len(current)>250: raise KnowledgeError("Knowledge section heading exceeds its supported length.")
        else: body.append(line)
    if body and "\n".join(body).strip(): sections.append((current,"\n".join(body).strip()))
    if fence is not None: raise KnowledgeError("Unclosed Markdown code fence.")
    if not sections: raise KnowledgeError("Knowledge document has no content.")
    chunks=[]
    for section,content in sections:
        prefix=f"{title}\n{section}\n\n"
        limit=MAX_CHUNK_CHARS-len(prefix)
        pieces=[]
        for paragraph in re.split(r"\n\s*\n",content):
            paragraph=paragraph.strip()
            while len(paragraph)>limit:
                cut=paragraph.rfind(" ",0,limit+1)
                if cut<limit//2: cut=limit
                pieces.append(paragraph[:cut].strip()); paragraph=paragraph[cut:].strip()
            if paragraph: pieces.append(paragraph)
        packed=[]; pending=""
        for piece in pieces:
            candidate=pending+"\n\n"+piece if pending else piece
            if len(candidate)>limit:
                packed.append(pending); pending=piece
            else: pending=candidate
        if pending: packed.append(pending)
        for body_text in packed:
            value=prefix+body_text; digest=sha256(value.encode()).hexdigest(); index=len(chunks)
            chunks.append(KnowledgeChunk(
                chunk_id=uuid5(NAMESPACE_URL,f"enterprise-ai-ops-agent/knowledge/{document_id}/{index}/{digest}"),
                document_id=document_id,source_name=source_name,source_path=f"knowledge/source/{document_id}.md",
                title=title,section=section,chunk_index=index,content=value,content_hash=digest,
            ))
    return chunks


def discover_documents(root: Path) -> list[Path]:
    if not root.is_dir(): raise KnowledgeError("Knowledge source directory is unavailable.")
    paths=sorted((path for path in root.rglob("*") if path.is_file()),key=lambda p:p.as_posix())
    if not paths: raise KnowledgeError("Knowledge source directory must not be empty.")
    if any(path.suffix.lower()!=".md" or path.is_symlink() or not path.resolve().is_relative_to(root.resolve()) for path in paths):
        raise KnowledgeError("Only in-root, non-symlink Markdown documents are supported.")
    return paths


def load_chunks(root: Path) -> list[KnowledgeChunk]:
    chunks=[]; seen=set()
    for path in discover_documents(root):
        identity=path.relative_to(root).with_suffix("").as_posix().casefold()
        if identity in seen: raise KnowledgeError("Duplicate logical document ID.")
        seen.add(identity)
        try: content=path.read_text(encoding="utf-8")
        except (OSError,UnicodeError): raise KnowledgeError("Knowledge document cannot be read as UTF-8.") from None
        if DISCLAIMER not in content: raise KnowledgeError("Demo corpus documents require the explicit project disclaimer.")
        chunks.extend(chunk_markdown(content,document_id=identity,source_name=path.name))
    return chunks
