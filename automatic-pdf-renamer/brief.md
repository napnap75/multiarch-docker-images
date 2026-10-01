	# Project Brief — Automatic PDF Renamer

	## 1. Vision

	A personal, 100% offline tool that watches a folder on a server, automatically identifies the type of each incoming PDF (invoice, purchase order, contract, etc.) from its content, applies the corresponding naming template by extracting the fields (date, counterparty, reference, amount), and moves the file into an organized folder structure. Low-confidence decisions go through human review in a local web dashboard. Every operation is logged and reversible.

	## 2. Problem and Context

	PDFs arrive continuously in a watched folder (emails, downloads, exports). Current naming is manual or inconsistent, making documents hard to find and archive. The user owns 50 to 500 files already correctly named according to 1 to 5 existing templates, which will serve as training and reference corpus.

	## 3. Objectives

	- Classify each incoming PDF against a known naming template, based on the file content (first-page text, header/footer), not the current filename.
	- Fill the fields of the selected template (date, counterparty, reference, amount) from the content.
	- Rename and move the file into an organized tree structure depending on the document type.
	- High confidence: automatic processing. Low confidence or incomplete extraction: queued for human review in the dashboard.
	- Log every decision (SQLite) with rollback capability and retraining from corrections.
	- Run fully offline, with no cloud APIs whatsoever.

	## 4. Scope

	### In scope (v1)

	- Folder watching (watcher) for new files only.
	- PDF text extraction (pdfplumber or PyMuPDF). All files are text-based, no OCR in v1.
	- Template classification: TF-IDF + Logistic Regression (scikit-learn) on the extracted text, with a configurable confidence threshold.
	- Per-template slot filling: regex + dateutil for dates, amounts, references; gazetteer + fuzzy matching (rapidfuzz) for counterparties, with spaCy/CamemBERT NER as fallback if needed.
	- Templates and extractors declared in configuration (YAML/JSON), not in code.
	- SQLite database: rename history, decisions, confidence scores, rollback target.
	- Local web dashboard: all processed files with status (not processed / processed / validated), file detail page with PDF preview, template and field correction, validation, history, statistics.
	- On-demand classifier retraining, using the existing corpus plus every file with "validated" status.

	### Out of scope (v1)

	- OCR of scanned documents (planned for v2 if the mix changes).
	- Backlog processing of existing files — new files only.
	- Remote language models, cloud APIs, telemetry.
	- Multi-user management, authentication.
	- Merging, splitting, or modifying PDF content.

	## 5. User and Usage

	Single user (personal use), technically proficient. Nominal flow:

	1. A PDF arrives in the watched folder.
	2. The watcher waits for the write to finish, extracts the text, classifies, extracts the fields, validates.
	3. High confidence and valid fields: the file is renamed and moved; the event is logged.
	4. Otherwise: the file appears in the dashboard review queue; the user corrects or validates; the correction feeds the training corpus.

	## 6. Functional Requirements

	- FR1: detect new PDFs in a configurable folder, waiting for file stability.
	- FR2: classify the document into one of the configured templates, with a confidence score and an "unknown" class below the threshold.
	- FR3: fill each template field according to its schema (type, format, validity range).
	- FR4: validate fields before renaming (parseable date, numeric amount, recognized or flagged counterparty); any failed validation routes to review.
	- FR5: rename and move to the template's target folder, resolving collisions (incremental suffix).
	- FR6: log every operation in SQLite with an old name → new name → decision mapping, and per-file status (not processed / processed / validated), enabling rollback and selecting training data.
	- FR7: local web dashboard (FastAPI + React): file list, detail page, review, correction, validation, history with undo, retraining trigger.
	- FR7a: the dashboard shows every processed file with its status: "not processed" (confidence was too low, rename pending), "processed" (automatically renamed and moved), or "validated" (the user confirmed or corrected the result — such files are eligible for future model retraining).
	- FR7b: clicking a file opens a detail page displaying the PDF, where the user can change the template if misclassified, correct the extracted field values, and validate the result. Validation sets the status to "validated" and feeds the corrected data into the training corpus.
	- FR8: adding a new template is done through a configuration entry (YAML) plus example files, with no code changes.

	## 7. Non-Functional Requirements

	- Privacy: 100% offline, no data ever leaves the server.
	- Performance: a standard-size PDF processed in under a few seconds.
	- Reliability: zero silent invalid renames — every ambiguous case goes to review.
	- Reversibility: every rename can be undone from the history.
	- Document language: French only in v1.
	- Stack: Python (FastAPI) backend, React frontend, SQLite, scikit-learn, spaCy/fr, pdfplumber or PyMuPDF, rapidfuzz, watchdog.
	- Portability: containerized deployment or systemd service on the user's server.

	## 8. Success Criteria

	- ≥ 95% correct template classification on typical files after initial training on the existing corpus.
	- ≥ 80% of files processed without human intervention (the rest in review, never a silent wrong rename).
	- Zero non-reversible renames.
	- Adding a new template in under 30 minutes without writing code.
	- Working v1 in under one month.

	## 9. Risks and Mitigations

	- Limited training corpus (50–500 files): start simple (TF-IDF + LogReg), optionally sentence-transformers embeddings if accuracy is insufficient; the review loop continuously enriches the corpus.
	- Templates too weakly distinguished in the text: enrich features (header, issuer, business keywords per template).
	- Poorly extracted or empty text files: systematic detection and routing to review.
	- Sensitive files moved by mistake: high default confidence threshold + strict validation + rollback.

	## 10. Milestones

	1. Week 1: text extraction, parsing the existing corpus into (template, fields), declarative template configuration.
	2. Week 2: classifier + extractors + validation + SQLite logging + watcher.
	3. Week 3: React dashboard (file list with statuses, detail page with PDF preview and correction, validation, history, rollback, retraining).
	4. Week 4: hardening (thresholds, collisions, tests), deployment, production go-live on the watched folder.
