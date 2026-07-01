;;; zeta-block.el --- Submit Zeta questions from any buffer -*- lexical-binding: t; -*-

;; This file is not part of GNU Emacs.

;;; Commentary:

;; Enable `zeta-block-mode', write a paragraph or comment block beginning with
;; "?", and press C-c C-c.  For inline prompts, write a line beginning with
;; "zeta?" or "zeta!" and press RET.  The package submits the instruction to
;; `zeta-block-rpc-command', registers live-buffer tools, and inserts Zeta's
;; status below the trigger.

;;; Code:

(require 'cl-lib)
(require 'json)
(require 'project)
(require 'subr-x)

(defgroup zeta-block nil
  "Submit Zeta questions from ordinary Emacs buffers."
  :group 'tools)

(defcustom zeta-block-rpc-command '("zeta" "rpc" "--stdio")
  "Command used to start the Zeta JSON-RPC backend.
Set this to an absolute executable path when developing against a local
checkout."
  :type '(repeat string)
  :group 'zeta-block)

(defcustom zeta-block-read-only-tools
  '("read" "grep" "ls" "query_log" "emacs_read")
  "Read-only tools made available to `?' block submissions."
  :type '(repeat string)
  :group 'zeta-block)

(defcustom zeta-block-inline-tools
  '("read" "grep" "ls" "query_log" "emacs_read" "emacs_replace")
  "Tools made available to inline `zeta!' edit submissions."
  :type '(repeat string)
  :group 'zeta-block)

(defcustom zeta-block-inline-question-triggers '("zeta?")
  "Prefixes that mark inline read-only Zeta questions."
  :type '(repeat string)
  :group 'zeta-block)

(defcustom zeta-block-inline-action-triggers '("zeta!")
  "Prefixes that mark inline Zeta edit/action instructions."
  :type '(repeat string)
  :group 'zeta-block)

(defface zeta-block-human-face
  '((t (:inherit shadow :underline t)))
  "Face used for human-authored Zeta prompts."
  :group 'zeta-block)

(defface zeta-block-agent-face
  '((t (:inherit highlight)))
  "Face used for agent-authored Zeta text."
  :group 'zeta-block)

(defvar zeta-block--process nil)
(defvar zeta-block--next-id 1)
(defvar zeta-block--callbacks (make-hash-table :test 'eql))
(defvar zeta-block--registered-tools (make-hash-table :test 'equal))
(defvar zeta-block--pending-runs (make-hash-table :test 'equal))
(defvar zeta-block--completed-runs (make-hash-table :test 'equal))
(defvar zeta-block--overlays nil)
(defvar zeta-block--active-buffer nil)
(defvar zeta-block--active-agent-prompt nil)
(defvar zeta-block--active-requests 0)
(defvar zeta-block--status 'off)
(defvar zeta-block--last-error nil)

(defvar zeta-block-mode-map
  (let ((map (make-sparse-keymap)))
    (define-key map (kbd "C-c C-c") #'zeta-block-submit)
    (define-key map (kbd "C-c z ?") #'zeta-block-ask-region)
    (define-key map (kbd "C-c z !") #'zeta-block-act-on-region)
    (define-key map (kbd "RET") #'zeta-block-return)
    map)
  "Keymap for `zeta-block-mode'.")

;;;###autoload
(define-minor-mode zeta-block-mode
  "Submit `?' blocks to Zeta with C-c C-c."
  :lighter (:eval (zeta-block-mode-line))
  :keymap zeta-block-mode-map)

;;;###autoload
(define-globalized-minor-mode zeta-block-global-mode
  zeta-block-mode
  zeta-block-mode)

;;;###autoload
(defun zeta-block-submit ()
  "Submit the current `?' block to Zeta and insert the answer below it.
When the current block is not a Zeta question, dispatch to the binding that
`C-c C-c' would have used without `zeta-block-mode'."
  (interactive)
  (pcase-let ((`(,begin ,end ,raw) (zeta-block-current-block)))
    (let* ((comment-prefix (zeta-block-comment-prefix raw))
           (question (zeta-block-clean-question raw)))
      (if (not question)
          (zeta-block-dispatch-original-c-c-c)
        (let ((placeholder (zeta-block-insert-placeholder end comment-prefix)))
          (zeta-block-add-overlay begin end 'human question)
          (setq zeta-block--active-buffer (current-buffer))
          (zeta-block-ask-async
           question
           (lambda (result error)
             (zeta-block-replace-placeholder
              placeholder
              (zeta-block-response-text result error)
              comment-prefix
              question))))))))

(defun zeta-block-dispatch-original-c-c-c ()
  "Call the command normally bound to `C-c C-c'."
  (let* ((zeta-block-mode nil)
         (command (key-binding (kbd "C-c C-c"))))
    (if (and command (not (eq command #'zeta-block-submit)))
        (call-interactively command)
      (user-error "Current block does not start with ?"))))

;;;###autoload
(defun zeta-block-return ()
  "Run the original RET command, then submit an inline Zeta instruction."
  (interactive)
  (zeta-block-dispatch-original-ret)
  (zeta-block-submit-inline-before-point))

(defun zeta-block-dispatch-original-ret ()
  "Call the command normally bound to RET."
  (let* ((zeta-block-mode nil)
         (command (key-binding (kbd "RET"))))
    (if (and command (not (eq command #'zeta-block-return)))
        (call-interactively command)
      (newline))))

(defun zeta-block-submit-inline-before-point ()
  "Submit the line before point when it contains an inline instruction."
  (when-let ((request (zeta-block-inline-request-before-point)))
    (let* ((kind (car request))
           (instruction (cdr request))
           (line-raw (zeta-block-previous-line-raw))
           (line-number (zeta-block-previous-line-number))
           (line-range (zeta-block-previous-line-range))
           (comment-prefix (zeta-block-comment-prefix line-raw))
           (placeholder (zeta-block-insert-placeholder (point) comment-prefix))
           (callback (lambda (result error)
                       (zeta-block-replace-placeholder
                        placeholder
                        (zeta-block-response-text result error)
                        comment-prefix
                        instruction))))
      (zeta-block-add-overlay (car line-range) (cdr line-range) 'human instruction)
      (setq zeta-block--active-buffer (current-buffer))
      (pcase kind
        ('question
         (zeta-block-ask-async
          (zeta-block-inline-question-objective instruction line-number)
          callback))
        ('action
         (zeta-block-inline-async instruction line-number callback))))))

;;;###autoload
(defun zeta-block-ask-region (begin end question)
  "Ask a read-only Zeta QUESTION about the active region from BEGIN to END."
  (interactive
   (if (use-region-p)
       (list (region-beginning)
             (region-end)
             (read-string "zeta? "))
     (user-error "Select a region first")))
  (zeta-block-submit-region begin end question 'question))

;;;###autoload
(defun zeta-block-act-on-region (begin end instruction)
  "Run a Zeta edit/action INSTRUCTION scoped to active region BEGIN..END."
  (interactive
   (if (use-region-p)
       (list (region-beginning)
             (region-end)
             (read-string "zeta! "))
     (user-error "Select a region first")))
  (zeta-block-submit-region begin end instruction 'action))

(defun zeta-block-submit-region (begin end instruction kind)
  "Submit region BEGIN..END with INSTRUCTION and request KIND."
  (let* ((scope (zeta-block-region-scope begin end))
         (placeholder (zeta-block-insert-placeholder
                       (zeta-block-scope-value scope 'end)
                       nil))
         (callback (lambda (result error)
                     (zeta-block-replace-placeholder
                      placeholder
                      (zeta-block-response-text result error)
                      nil
                      instruction))))
    (zeta-block-add-overlay begin end 'human instruction)
    (setq zeta-block--active-buffer (current-buffer))
    (pcase kind
      ('question
       (zeta-block-ask-async
        (zeta-block-region-question-objective instruction scope)
        callback))
      ('action
       (zeta-block-region-async instruction scope callback)))))

(defun zeta-block-inline-instruction-before-point ()
  "Return the cleaned inline instruction before point, or nil.
This preserves the old helper API by dropping the request kind."
  (cdr-safe (zeta-block-inline-request-before-point)))

(defun zeta-block-inline-request-before-point ()
  "Return (KIND . INSTRUCTION) for the previous line, or nil."
  (let* ((raw (zeta-block-previous-line-raw))
         (text (zeta-block-clean-line raw)))
    (or (zeta-block-inline-request-for-triggers
         text
         zeta-block-inline-question-triggers
         'question)
        (zeta-block-inline-request-for-triggers
         text
         zeta-block-inline-action-triggers
         'action))))

(defun zeta-block-inline-request-for-triggers (text triggers kind)
  "Return a KIND request when TEXT starts with one of TRIGGERS."
  (catch 'request
    (dolist (trigger triggers)
      (when-let ((instruction (zeta-block-inline-instruction-after-trigger
                               text
                               trigger)))
        (throw 'request (cons kind instruction))))
    nil))

(defun zeta-block-inline-instruction-after-trigger (text trigger)
  "Return instruction in TEXT after TRIGGER, or nil."
  (when (and (stringp trigger)
             (not (string-empty-p trigger))
             (string-prefix-p trigger text))
    (let ((rest (substring text (length trigger))))
      (when (or (string-empty-p rest)
                (member (substring rest 0 1) '(" " "\t")))
        (let ((instruction (string-trim rest)))
          (unless (string-empty-p instruction)
            instruction))))))

(defun zeta-block-previous-line-raw ()
  "Return the raw text of the line before point."
  (save-excursion
    (forward-line -1)
    (buffer-substring-no-properties
     (line-beginning-position)
     (line-end-position))))

(defun zeta-block-previous-line-number ()
  "Return the one-indexed line number before point."
  (save-excursion
    (forward-line -1)
    (line-number-at-pos)))

(defun zeta-block-previous-line-range ()
  "Return the buffer range for the line before point."
  (save-excursion
    (forward-line -1)
    (cons (line-beginning-position) (line-end-position))))

(defun zeta-block-region-scope (begin end)
  "Return normalized whole-line scope metadata for region BEGIN..END."
  (let* ((range (zeta-block-normalized-region-line-range begin end))
         (start (car range))
         (finish (cdr range))
         (start-line (line-number-at-pos start))
         (end-line (line-number-at-pos finish))
         (text (buffer-substring-no-properties start finish))
         (hash (secure-hash 'sha256 text)))
    (list
     (cons 'start start)
     (cons 'end finish)
     (cons 'start-line start-line)
     (cons 'end-line end-line)
     (cons 'text text)
     (cons 'hash (concat "sha256:" hash)))))

(defun zeta-block-normalized-region-line-range (begin end)
  "Return whole-line cons range for region BEGIN..END."
  (save-excursion
    (let ((start (min begin end))
          (finish (max begin end)))
      (goto-char start)
      (setq start (line-beginning-position))
      (goto-char finish)
      (when (and (> finish start)
                 (= finish (line-beginning-position)))
        (forward-line -1))
      (cons start (line-end-position)))))

(defun zeta-block-scope-value (scope key)
  "Return KEY from SCOPE."
  (cdr (assq key scope)))

(defun zeta-block-current-block ()
  "Return the current paragraph as (BEGIN END RAW-TEXT)."
  (save-excursion
    (let (begin end)
      (backward-paragraph)
      (setq begin (point))
      (forward-paragraph)
      (setq end (point))
      (list begin end (buffer-substring-no-properties begin end)))))

(defun zeta-block-clean-question (raw)
  "Return the question in RAW, or nil when RAW is not a `?' block."
  (let* ((lines (split-string raw "\n"))
         (cleaned-lines (mapcar #'zeta-block-clean-line lines))
         (text (string-trim (string-join cleaned-lines "\n"))))
    (when (string-prefix-p "?" text)
      (string-trim (substring text 1)))))

(defun zeta-block-clean-line (line)
  "Strip whitespace and common line comment prefixes from LINE."
  (let ((text (string-trim-left line)))
    (cond
     ((string-match-p "\\`;;" text)
      (string-trim-left (substring text 2)))
     ((string-match-p "\\`//" text)
      (string-trim-left (substring text 2)))
     ((string-match-p "\\`#" text)
      (string-trim-left (substring text 1)))
     ((string-match-p "\\`;" text)
      (string-trim-left (substring text 1)))
     (t text))))

(defun zeta-block-comment-prefix (raw)
  "Return the comment prefix to use for a response to RAW, or nil."
  (let ((prefix nil))
    (dolist (line (split-string raw "\n") prefix)
      (let ((text (string-trim-left line)))
        (cond
         ((and (null prefix) (string-prefix-p ";;" text))
          (setq prefix ";;"))
         ((and (null prefix) (string-prefix-p "//" text))
          (setq prefix "//"))
         ((and (null prefix) (string-prefix-p "#" text))
          (setq prefix "#"))
         ((and (null prefix) (string-prefix-p ";" text))
          (setq prefix ";")))))))

(defun zeta-block-format-response (text comment-prefix)
  "Format TEXT as a Zeta response using COMMENT-PREFIX when non-nil."
  (let* ((body (string-trim-right text))
         (lines (split-string body "\n"))
         (prefix (and comment-prefix
                      (if (string-empty-p comment-prefix)
                          nil
                        comment-prefix))))
    (if prefix
        (concat
         prefix " Zeta:\n"
         (mapconcat (lambda (line) (concat prefix "   " line)) lines "\n")
         "\n")
      (concat
       "Zeta:\n"
       (mapconcat (lambda (line) (concat "  " line)) lines "\n")
       "\n"))))

(defun zeta-block-add-overlay (start end origin prompt &optional run-id)
  "Tag START..END with an overlay for ORIGIN and PROMPT.
ORIGIN is the symbol `human' or `agent'."
  (when (< start end)
    (let ((overlay (make-overlay start end nil t nil)))
      (overlay-put overlay 'zeta-origin origin)
      (overlay-put overlay 'zeta-prompt prompt)
      (when run-id
        (overlay-put overlay 'zeta-run-id run-id))
      (overlay-put overlay 'face
                   (pcase origin
                     ('human 'zeta-block-human-face)
                     ('agent 'zeta-block-agent-face)
                     (_ nil)))
      (overlay-put overlay 'help-echo
                   (zeta-block-overlay-help origin prompt run-id))
      (push overlay zeta-block--overlays)
      overlay)))

(defun zeta-block-overlay-help (origin prompt run-id)
  "Return tooltip text for an overlay with ORIGIN, PROMPT, and RUN-ID."
  (string-join
   (delq nil
         (list
          (format "zeta origin: %s" origin)
          (and run-id (format "run: %s" run-id))
          (and prompt (format "prompt: %s" prompt))))
   "\n"))

;;;###autoload
(defun zeta-block-clear-overlays ()
  "Delete all Zeta provenance overlays in live buffers."
  (interactive)
  (mapc #'delete-overlay zeta-block--overlays)
  (setq zeta-block--overlays nil))

(defun zeta-block-insert-placeholder (position comment-prefix)
  "Insert a thinking placeholder after POSITION and return replacement markers."
  (save-excursion
    (goto-char position)
    (unless (bolp)
      (insert "\n"))
    (insert "\n")
    (let ((start (point-marker)))
      (insert (zeta-block-format-response "thinking..." comment-prefix))
      (let ((end (point-marker)))
        (set-marker-insertion-type start nil)
        (set-marker-insertion-type end t)
        (cons start end)))))

(defun zeta-block-replace-placeholder (markers text comment-prefix &optional prompt)
  "Replace MARKERS with TEXT formatted using COMMENT-PREFIX.
When PROMPT is non-nil, tag the inserted response as agent-authored."
  (let ((start (car markers))
        (end (cdr markers)))
    (when (and (markerp start)
               (markerp end)
               (marker-buffer start)
               (eq (marker-buffer start) (marker-buffer end))
               (< (marker-position start) (marker-position end)))
      (let ((buffer (marker-buffer start))
            (selected-window (selected-window))
            (selected-buffer (current-buffer))
            (selected-point (point)))
        (with-current-buffer buffer
          (let ((buffer-point (point))
                (windows (get-buffer-window-list buffer nil t)))
            (unwind-protect
                (save-excursion
                  (goto-char (marker-position start))
                  (delete-region start end)
                  (let ((insert-start (point)))
                    (insert (zeta-block-format-response text comment-prefix))
                    (zeta-block-add-overlay insert-start (point) 'agent prompt)))
              (goto-char (min buffer-point (point-max)))
              (dolist (window windows)
                (when (window-live-p window)
                  (set-window-point
                   window
                   (min (window-point window) (point-max))))))))
        (when (and (window-live-p selected-window)
                   (buffer-live-p selected-buffer))
          (select-window selected-window)
          (with-current-buffer selected-buffer
            (goto-char (min selected-point (point-max)))))))))

(defun zeta-block-response-text (result error)
  "Return display text for RESULT or ERROR."
  (cond
   (error
    (format "Error: %s" error))
   ((and (hash-table-p result) (gethash "final_answer" result))
    (gethash "final_answer" result))
   ((and (hash-table-p result) (gethash "final_text" result))
    (gethash "final_text" result))
   ((and (listp result) (alist-get 'final_answer result))
    (alist-get 'final_answer result))
   ((and (listp result) (alist-get 'final_text result))
    (alist-get 'final_text result))
   (t "No final answer.")))

(defun zeta-block-ask-async (question callback)
  "Ask Zeta QUESTION and call CALLBACK with (RESULT ERROR)."
  (zeta-block-ensure-process)
  (zeta-block-send-request
   "session.run"
   `(("workflow" . "ask")
     ("objective" . ,question)
     ("tools" . ,(vconcat zeta-block-read-only-tools))
     ("system" . ,(zeta-block-ask-system-prompt)))
   (lambda (result error)
     (zeta-block-handle-session-start result error callback))
   t))

(defun zeta-block-inline-question-objective (question line-number)
  "Return the model objective for an inline QUESTION on LINE-NUMBER."
  (format
   (string-join
    '("Inline editor question:"
      "%s"
      ""
      "Question line: %s"
      ""
      "The current live Emacs buffer is the primary context."
      "The zeta? line is a command, not document prose."
      "Interpret relative references such as previous paragraph relative to the question line."
      "Use emacs_read when the live buffer is relevant.")
    "\n")
   question
   line-number))

(defun zeta-block-region-question-objective (question scope)
  "Return the model objective for a region-scoped QUESTION."
  (format
   (string-join
    '("Region-scoped editor question:"
      "%s"
      ""
      "Selected region: lines %s..%s"
      "Selected text hash: %s"
      ""
      "The selected region is the primary context."
      "Use surrounding buffer context only if needed to answer accurately."
      "Use emacs_read to inspect the live buffer.")
    "\n")
   question
   (zeta-block-scope-value scope 'start-line)
   (zeta-block-scope-value scope 'end-line)
   (zeta-block-scope-value scope 'hash)))

(defun zeta-block-inline-async (instruction line-number callback)
  "Run inline Zeta INSTRUCTION from LINE-NUMBER and call CALLBACK."
  (zeta-block-ensure-process)
  (setq zeta-block--active-agent-prompt instruction)
  (zeta-block-register-tools (list (zeta-block-emacs-replace-tool)))
  (zeta-block-send-request
   "session.run"
   `(("workflow" . "do")
     ("objective" . ,(zeta-block-inline-objective instruction line-number))
     ("tools" . ,(vconcat zeta-block-inline-tools))
     ("system" . ,(zeta-block-inline-system-prompt)))
   (lambda (result error)
     (zeta-block-handle-session-start result error callback))
   t))

(defun zeta-block-region-async (instruction scope callback)
  "Run region-scoped Zeta INSTRUCTION over SCOPE and call CALLBACK."
  (zeta-block-ensure-process)
  (setq zeta-block--active-agent-prompt instruction)
  (zeta-block-register-tools (list (zeta-block-emacs-replace-tool)))
  (zeta-block-send-request
   "session.run"
   `(("workflow" . "do")
     ("objective" . ,(zeta-block-region-objective instruction scope))
     ("tools" . ,(vconcat zeta-block-inline-tools))
     ("system" . ,(zeta-block-region-system-prompt)))
   (lambda (result error)
     (zeta-block-handle-session-start result error callback))
   t))

(defun zeta-block-inline-objective (instruction line-number)
  "Return the model objective for an inline INSTRUCTION on LINE-NUMBER."
  (format
   (string-join
    '("Inline editor instruction:"
      "%s"
      ""
      "Instruction line: %s"
      ""
      "The current live Emacs buffer is the primary document."
      "The zeta! instruction line is a command, not document prose."
      "Interpret relative references such as previous paragraph relative to the instruction line."
      "Read it with emacs_read before deciding what to change."
      "If a document change is requested, apply it to the live buffer with emacs_replace."
      "emacs_read includes line-number prefixes for reference; pass old/new text to emacs_replace without those prefixes.")
    "\n")
   instruction
   line-number))

(defun zeta-block-region-objective (instruction scope)
  "Return the model objective for region-scoped INSTRUCTION over SCOPE."
  (format
   (string-join
    '("Region-scoped editor instruction:"
      "%s"
      ""
      "Selected region: lines %s..%s"
      "Selected text hash: %s"
      ""
      "Only edit the selected region unless the user explicitly asks otherwise."
      "Use surrounding buffer context only to preserve meaning, style, and references."
      "Read the live buffer with emacs_read before deciding what to change."
      "Apply changes with emacs_replace to the selected line range."
      "emacs_read includes line-number prefixes for reference; pass old/new text to emacs_replace without those prefixes.")
    "\n")
   instruction
   (zeta-block-scope-value scope 'start-line)
   (zeta-block-scope-value scope 'end-line)
   (zeta-block-scope-value scope 'hash)))

(defun zeta-block-handle-session-start (result error callback)
  "Track an async session RESULT or report ERROR to CALLBACK."
  (cond
   (error
    (zeta-block-clear-running error)
    (funcall callback nil error))
   ((not (hash-table-p result))
    (zeta-block-clear-running "session.run returned an invalid result")
    (funcall callback nil "session.run returned an invalid result"))
   ((not (string= (or (gethash "status" result) "") "started"))
    (zeta-block-clear-running)
    (funcall callback result nil))
   (t
    (let ((run-id (gethash "run_id" result)))
      (if (not (and (stringp run-id) (not (string-empty-p run-id))))
          (progn
            (zeta-block-clear-running "session.run did not return a run_id")
            (funcall callback nil "session.run did not return a run_id"))
        (if-let ((completed (gethash run-id zeta-block--completed-runs)))
            (progn
              (remhash run-id zeta-block--completed-runs)
              (zeta-block-clear-running (cdr completed))
              (funcall callback (car completed) (cdr completed)))
          (puthash run-id callback zeta-block--pending-runs)))))))

(defun zeta-block-ask-system-prompt ()
  "Return the read-only system prompt used by `?' blocks."
  (string-join
   '("Answer concisely from the available project and editor context."
     "Use only read-only tools."
     "Use emacs_read when the live current buffer is relevant, because it can include unsaved edits."
     "Do not propose shell commands or mutations.")
   " "))

(defun zeta-block-inline-system-prompt ()
  "Return the system prompt used by inline `zeta!' instructions."
  (string-join
   '("You are editing the user's current Emacs buffer."
     "Use emacs_read to inspect the live buffer, including unsaved edits."
     "Use emacs_replace for requested document changes."
     "When using emacs_replace, old must exactly match the current unnumbered text in the requested line range."
     "Keep edits scoped to the inline instruction."
     "Do not use shell commands."
     "When finished, return a brief summary of what changed.")
   " "))

(defun zeta-block-region-system-prompt ()
  "Return the system prompt used by selected-region `zeta!' instructions."
  (string-join
   '("You are editing a selected region in the user's current Emacs buffer."
     "The selected region is the edit scope."
     "Use emacs_read to inspect the live buffer, including unsaved edits."
     "Use emacs_replace for requested document changes."
     "When using emacs_replace, old must exactly match the current unnumbered text in the requested line range."
     "Do not edit outside the selected region unless the user explicitly asks."
     "Do not use shell commands."
     "When finished, return a brief summary of what changed.")
   " "))

(defun zeta-block-ensure-process ()
  "Start the Zeta JSON-RPC subprocess if needed."
  (unless (process-live-p zeta-block--process)
    (setq zeta-block--process
          (make-process
           :name "zeta-block-rpc"
           :buffer "*zeta-block-rpc*"
           :command zeta-block-rpc-command
           :connection-type 'pipe
           :coding 'utf-8
           :noquery t
           :filter #'zeta-block--process-filter
           :sentinel #'zeta-block--process-sentinel))
    (setq zeta-block--status 'idle
          zeta-block--last-error nil)
    (force-mode-line-update t)
    (process-put zeta-block--process 'zeta-block-partial "")
    (zeta-block-send-request "initialize" nil nil)
    (clrhash zeta-block--registered-tools)
    (zeta-block-register-tools (list (zeta-block-emacs-read-tool)))))

(defun zeta-block-register-tools (tools)
  "Register RPC client TOOLS that have not already been registered."
  (let ((new-tools nil))
    (dolist (tool tools)
      (let ((name (zeta-block-tool-name tool)))
        (when (and name (not (gethash name zeta-block--registered-tools)))
          (puthash name t zeta-block--registered-tools)
          (push tool new-tools))))
    (when new-tools
      (zeta-block-send-request
       "tools.register"
       `(("tools" . ,(vconcat (nreverse new-tools))))
       nil))))

(defun zeta-block-tool-name (tool)
  "Return TOOL's name from a JSON alist."
  (cdr (assoc "name" tool)))

;;;###autoload
(defun zeta-block-restart ()
  "Restart the Zeta JSON-RPC subprocess used by `zeta-block-mode'."
  (interactive)
  (when (process-live-p zeta-block--process)
    (delete-process zeta-block--process))
  (setq zeta-block--process nil)
  (clrhash zeta-block--callbacks)
  (clrhash zeta-block--pending-runs)
  (clrhash zeta-block--completed-runs)
  (setq zeta-block--active-requests 0
        zeta-block--status 'off
        zeta-block--last-error nil)
  (zeta-block-ensure-process)
  (message "zeta-block restarted"))

(defun zeta-block-emacs-read-tool ()
  "Return the JSON-RPC descriptor for the Emacs read tool."
  '(("name" . "emacs_read")
    ("description" . "Read numbered lines from the current live Emacs buffer, including unsaved edits.")
    ("schema" . (("type" . "object")
                 ("additionalProperties" . :json-false)
                 ("properties" . (("start_line" . (("type" . "integer")
                                                   ("minimum" . 1)))
                                  ("end_line" . (("type" . "integer")
                                                 ("minimum" . 1)))))))))

(defun zeta-block-emacs-replace-tool ()
  "Return the JSON-RPC descriptor for the Emacs replacement tool."
  '(("name" . "emacs_replace")
    ("description" . "Replace an exact line range in the current live Emacs buffer. The old text must match the current buffer contents.")
    ("schema" . (("type" . "object")
                 ("additionalProperties" . :json-false)
                 ("required" . ["start_line" "end_line" "old" "new"])
                 ("properties" . (("start_line" . (("type" . "integer")
                                                   ("minimum" . 1)))
                                  ("end_line" . (("type" . "integer")
                                                 ("minimum" . 1)))
                                  ("old" . (("type" . "string")))
                                  ("new" . (("type" . "string")))))))))

;;;###autoload
(defun zeta-block-status ()
  "Show the current Zeta block subprocess status."
  (interactive)
  (message
   "Zeta %s%s%s"
   (pcase zeta-block--status
     ('off "off")
     ('idle "idle")
     ('running "running")
     ('error "error")
     (_ (symbol-name zeta-block--status)))
   (if (process-live-p zeta-block--process)
       (format " · pid %s" (process-id zeta-block--process))
     "")
   (cond
    ((> zeta-block--active-requests 0)
     (format " · active requests %d" zeta-block--active-requests))
    (zeta-block--last-error
     (format " · %s" zeta-block--last-error))
    (t ""))))

(defun zeta-block-mode-line ()
  "Return the mode-line lighter for `zeta-block-mode'."
  (pcase zeta-block--status
    ('off " Zeta:off")
    ('idle " Zeta:idle")
    ('running " Zeta:run")
    ('error " Zeta:err")
    (_ " Zeta:?")))

(defun zeta-block-set-running ()
  "Mark one user-visible Zeta request as running."
  (setq zeta-block--active-requests (1+ zeta-block--active-requests)
        zeta-block--status 'running
        zeta-block--last-error nil)
  (force-mode-line-update t))

(defun zeta-block-clear-running (&optional error)
  "Mark one user-visible Zeta request as done, optionally with ERROR."
  (setq zeta-block--active-requests (max 0 (1- zeta-block--active-requests)))
  (cond
   (error
    (setq zeta-block--status 'error
          zeta-block--last-error error))
   ((> zeta-block--active-requests 0)
    (setq zeta-block--status 'running))
   ((process-live-p zeta-block--process)
    (setq zeta-block--status 'idle))
   (t
    (setq zeta-block--status 'off)))
  (force-mode-line-update t))

(defun zeta-block-send-request (method params callback &optional track)
  "Send JSON-RPC METHOD with PARAMS and optional CALLBACK."
  (let ((id zeta-block--next-id))
    (setq zeta-block--next-id (1+ zeta-block--next-id))
    (when track
      (zeta-block-set-running))
    (when callback
      (puthash id (cons callback track) zeta-block--callbacks))
    (zeta-block-send-message
     `(("jsonrpc" . "2.0")
       ("id" . ,id)
       ("method" . ,method)
       ,@(when params `(("params" . ,params)))))))

(defun zeta-block-send-notification (method params)
  "Send JSON-RPC notification METHOD with PARAMS."
  (zeta-block-send-message
   `(("jsonrpc" . "2.0")
     ("method" . ,method)
     ("params" . ,params))))

(defun zeta-block-send-message (message)
  "Send JSON-RPC MESSAGE to the Zeta subprocess."
  (process-send-string
   zeta-block--process
   (concat (json-encode message) "\n")))

(defun zeta-block--process-filter (process chunk)
  "Handle JSON-RPC CHUNK from PROCESS."
  (let* ((partial (or (process-get process 'zeta-block-partial) ""))
         (text (concat partial chunk))
         (lines (split-string text "\n")))
    (process-put process 'zeta-block-partial
                 (if (string-suffix-p "\n" text) "" (car (last lines))))
    (dolist (line (if (string-suffix-p "\n" text) lines (butlast lines)))
      (unless (string-empty-p (string-trim line))
        (zeta-block-handle-message line)))))

(defun zeta-block-handle-message (line)
  "Handle one JSON-RPC message LINE."
  (condition-case err
      (let* ((json-object-type 'hash-table)
             (json-array-type 'list)
             (message (json-read-from-string line)))
        (cond
         ((gethash "id" message)
          (zeta-block-handle-response message))
         ((string= (gethash "method" message) "tools.call")
          (zeta-block-handle-tool-call (gethash "params" message)))
         ((string= (gethash "method" message) "events.notify")
          (zeta-block-handle-event-notify (gethash "params" message)))
         ((string= (gethash "method" message) "events.publish")
          nil)))
    (error
     (message "zeta-block: invalid JSON-RPC message: %s" err))))

(defun zeta-block-handle-response (message)
  "Dispatch JSON-RPC response MESSAGE to its callback."
  (let* ((id (gethash "id" message))
         (entry (gethash id zeta-block--callbacks))
         (callback (car-safe entry))
         (track (cdr-safe entry)))
    (when entry
      (remhash id zeta-block--callbacks)
      (if-let ((error (gethash "error" message)))
          (let ((message (or (gethash "message" error) "JSON-RPC error")))
            (when track
              (zeta-block-clear-running message))
            (funcall callback nil message))
        (when (and track
                   (not (zeta-block-session-start-result-p
                         (gethash "result" message))))
          (zeta-block-clear-running))
        (funcall callback (gethash "result" message) nil)))))

(defun zeta-block-session-start-result-p (result)
  "Return non-nil when RESULT is the async `session.run' start response."
  (and (hash-table-p result)
       (string= (or (gethash "status" result) "") "started")
       (stringp (gethash "run_id" result))))

(defun zeta-block-handle-event-notify (params)
  "Handle an `events.notify' notification with PARAMS."
  (when (hash-table-p params)
    (let ((event (gethash "event" params)))
      (when (hash-table-p event)
        (zeta-block-handle-run-event event)))))

(defun zeta-block-handle-run-event (event)
  "Complete pending run state from terminal lifecycle EVENT."
  (let* ((event-type (gethash "event_type" event))
         (run-id (gethash "run_id" event))
         (payload (gethash "payload" event))
         (target-agent (and (hash-table-p payload)
                            (gethash "target_agent" payload)))
         (callback (and (stringp run-id)
                        (gethash run-id zeta-block--pending-runs))))
    (when (and (stringp run-id)
               (member event-type
                       '("runtime.queue_item.completed"
                         "runtime.queue_item.failed"
                         "runtime.queue_item.cancelled"
                         "runtime.queue_item.unhandled"))
               (string= target-agent "zeta.session.turn"))
      (let* ((result (and (hash-table-p payload)
                          (gethash "result" payload)))
             (error (zeta-block-terminal-event-error event result)))
        (if callback
            (progn
              (remhash run-id zeta-block--pending-runs)
              (zeta-block-clear-running error)
              (funcall callback result error))
          (puthash run-id (cons result error) zeta-block--completed-runs))))))

(defun zeta-block-terminal-event-error (event result)
  "Return displayable error text for terminal EVENT and RESULT, or nil."
  (let ((event-type (gethash "event_type" event)))
    (cond
     ((string= event-type "runtime.queue_item.completed")
      nil)
     ((and (hash-table-p result)
           (gethash "error" result))
      (format "%s" (gethash "error" result)))
     ((hash-table-p result)
      (format "Run %s." (or (gethash "outcome" result) "failed")))
     (t
      (format "Run %s." (car (last (split-string event-type "\\."))))))))

(defun zeta-block-handle-tool-call (params)
  "Handle a server tool call with PARAMS."
  (let* ((id (and (hash-table-p params) (gethash "id" params)))
         (name (and (hash-table-p params) (gethash "name" params)))
         (arguments (and (hash-table-p params) (gethash "arguments" params)))
         (result
          (cond
           ((string= name "emacs_read")
            (zeta-block-emacs-read arguments))
           ((string= name "emacs_replace")
            (zeta-block-emacs-replace arguments))
           (t
            `(("ok" . :json-false)
              ("error" . (("code" . "unknown-tool")
                          ("message" . ,(format "unknown Emacs tool: %s" name)))))))))
    (when id
      (zeta-block-send-notification
       "tools.respond"
       `(("id" . ,id)
         ("result" . ,result))))))

(defun zeta-block-emacs-read (arguments)
  "Return a tool result for reading the active Emacs buffer.
ARGUMENTS may contain start_line and end_line."
  (let ((buffer zeta-block--active-buffer))
    (if (not (buffer-live-p buffer))
        '(("ok" . :json-false)
          ("error" . (("code" . "buffer-unavailable")
                      ("message" . "No active Emacs buffer is available."))))
      (with-current-buffer buffer
        (let* ((range (zeta-block-buffer-range arguments))
               (start-line (line-number-at-pos (car range)))
               (end-line (line-number-at-pos (cdr range)))
               (text (zeta-block-numbered-text (car range) (cdr range)))
               (path (or buffer-file-name ""))
               (hash (secure-hash 'sha256 (buffer-substring-no-properties
                                           (point-min)
                                           (point-max)))))
          (list
           (cons "ok" t)
           (cons "content" (vector (list (cons "type" "text")
                                         (cons "text" text))))
           (cons "metadata"
                 (list
                  (cons "buffer_name" (buffer-name))
                  (cons "path" path)
                  (cons "modified" (if (buffer-modified-p) t :json-false))
                  (cons "modified_tick" (buffer-chars-modified-tick))
                  (cons "content_hash" (concat "sha256:" hash))
                  (cons "start_line" start-line)
                  (cons "end_line" end-line)))))))))

(defun zeta-block-emacs-replace (arguments)
  "Return a tool result for replacing text in the active Emacs buffer."
  (let ((buffer zeta-block--active-buffer))
    (cond
     ((not (buffer-live-p buffer))
      '(("ok" . :json-false)
        ("error" . (("code" . "buffer-unavailable")
                    ("message" . "No active Emacs buffer is available.")))))
     ((not (hash-table-p arguments))
      '(("ok" . :json-false)
        ("error" . (("code" . "invalid-arguments")
                    ("message" . "emacs_replace arguments must be an object.")))))
     (t
      (with-current-buffer buffer
        (let* ((start-line (gethash "start_line" arguments))
               (end-line (gethash "end_line" arguments))
               (old (gethash "old" arguments))
               (new (gethash "new" arguments)))
          (cond
           ((not (and (integerp start-line)
                      (integerp end-line)
                      (<= start-line end-line)
                      (stringp old)
                      (stringp new)))
            '(("ok" . :json-false)
              ("error" . (("code" . "invalid-arguments")
                          ("message" . "emacs_replace requires start_line, end_line, old, and new.")))))
           (t
            (zeta-block-apply-line-replacement
             start-line
             end-line
             old
             new)))))))))

(defun zeta-block-apply-line-replacement (start-line end-line old new)
  "Replace START-LINE..END-LINE when OLD matches current buffer text."
  (let* ((start (zeta-block-line-position start-line))
         (end (save-excursion
                (goto-char (zeta-block-line-position end-line))
                (line-end-position)))
         (current (buffer-substring-no-properties start end)))
    (if (not (string= current old))
        `(("ok" . :json-false)
          ("error" . (("code" . "stale-buffer")
                      ("message" . "The requested line range no longer matches old text; read the buffer again.")))
          ("metadata" . (("start_line" . ,start-line)
                         ("end_line" . ,end-line)
                         ("current" . ,current))))
      (let ((before-hash (secure-hash 'sha256 (buffer-substring-no-properties
                                               (point-min)
                                               (point-max)))))
        (save-excursion
          (goto-char start)
          (delete-region start end)
          (insert new)
          (zeta-block-add-overlay
           start
           (point)
           'agent
           zeta-block--active-agent-prompt))
        (let ((after-hash (secure-hash 'sha256 (buffer-substring-no-properties
                                                (point-min)
                                                (point-max)))))
          `(("ok" . t)
            ("content" . [,(list (cons "type" "text")
                                 (cons "text"
                                       (format "replaced lines %d..%d in %s"
                                               start-line
                                               end-line
                                               (buffer-name))))])
            ("metadata" . (("buffer_name" . ,(buffer-name))
                           ("path" . ,(or buffer-file-name ""))
                           ("start_line" . ,start-line)
                           ("end_line" . ,end-line)
                           ("before_hash" . ,(concat "sha256:" before-hash))
                           ("after_hash" . ,(concat "sha256:" after-hash))))))))))

(defun zeta-block-numbered-text (start end)
  "Return buffer text between START and END with one-indexed line numbers."
  (save-excursion
    (goto-char start)
    (let ((lines nil))
      (while (< (point) end)
        (let ((line (line-number-at-pos))
              (line-text (buffer-substring-no-properties
                          (line-beginning-position)
                          (min (line-end-position) end))))
          (push (format "%d: %s" line line-text) lines))
        (forward-line 1))
      (string-join (nreverse lines) "\n"))))

(defun zeta-block-buffer-range (arguments)
  "Return cons of buffer positions requested by ARGUMENTS."
  (let* ((start-line (and (hash-table-p arguments)
                          (gethash "start_line" arguments)))
         (end-line (and (hash-table-p arguments)
                        (gethash "end_line" arguments)))
         (start (if (integerp start-line)
                    (zeta-block-line-position start-line)
                  (point-min)))
         (end (if (integerp end-line)
                  (save-excursion
                    (goto-char (zeta-block-line-position end-line))
                    (line-end-position))
                (point-max))))
    (cons start end)))

(defun zeta-block-line-position (line)
  "Return buffer position at one-indexed LINE."
  (save-excursion
    (goto-char (point-min))
    (forward-line (max 0 (1- line)))
    (point)))

(defun zeta-block--process-sentinel (process event)
  "Handle PROCESS lifecycle EVENT."
  (unless (process-live-p process)
    (maphash
     (lambda (_id entry)
       (let ((callback (car-safe entry)))
         (when callback
           (funcall callback nil (string-trim (format "Zeta process %s" event))))))
     zeta-block--callbacks)
    (maphash
     (lambda (_run-id callback)
       (when callback
         (funcall callback nil (string-trim (format "Zeta process %s" event)))))
     zeta-block--pending-runs)
    (clrhash zeta-block--callbacks)
    (clrhash zeta-block--pending-runs)
    (clrhash zeta-block--completed-runs)
    (setq zeta-block--active-requests 0
          zeta-block--status (if (string-match-p "finished" event) 'off 'error)
          zeta-block--last-error (unless (eq zeta-block--status 'off)
                                   (string-trim (format "Zeta process %s" event))))
    (force-mode-line-update t)
    (when (eq process zeta-block--process)
      (setq zeta-block--process nil))))

(provide 'zeta-block)

;;; zeta-block.el ends here
