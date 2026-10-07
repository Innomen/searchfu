"""Index-time document extraction only. No search imports this module.

Scanned PDFs require a separate OCR ingest step; no OCR is launched implicitly.
Legacy binary Office formats are deliberately unsupported.
"""
from pathlib import Path
import zipfile
import xml.etree.ElementTree as ET

DOCUMENT_EXTS={'.pdf','.docx'}
MAX_DOCUMENT_BYTES=64*1024*1024


def extract(path):
    path=Path(path)
    if path.stat().st_size>MAX_DOCUMENT_BYTES: raise ValueError('document_size_limit')
    if path.suffix.lower()=='.pdf':
        from pypdf import PdfReader
        reader=PdfReader(path)
        if reader.is_encrypted: raise ValueError('encrypted_pdf')
        if len(reader.pages)>2000: raise ValueError('document_page_limit')
        parts=[];total=0
        for page in reader.pages:
            text=page.extract_text() or '';total+=len(text)
            if total>MAX_DOCUMENT_BYTES: raise ValueError('document_text_limit')
            parts.append(text)
        text='\n\n'.join(parts)
        if not text.strip(): raise ValueError('pdf_requires_ocr_or_empty')
        return text,'pdf-text-v1'
    if path.suffix.lower()=='.docx':
        with zipfile.ZipFile(path) as archive:
            info=archive.getinfo('word/document.xml')
            if info.file_size>MAX_DOCUMENT_BYTES: raise ValueError('document_text_limit')
            xml=archive.read(info)
        # Do not process entity declarations or external resources.
        decoded=xml.decode('utf-8-sig')
        if '<!DOCTYPE' in decoded.upper() or '<!ENTITY' in decoded.upper(): raise ValueError('document_xml_entities')
        root=ET.fromstring(decoded);ns='{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
        return '\n\n'.join(''.join(p.itertext()) for p in root.iter(ns+'p')),'docx-text-v1'
    raise ValueError('unsupported_document_format')
