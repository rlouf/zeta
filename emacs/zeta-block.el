;;; zeta-block.el --- Submit Zeta questions from any buffer -*- lexical-binding: t; -*-

;; This file is not part of GNU Emacs.

;;; Commentary:

;; Enable `zeta-block-mode', write a paragraph or comment block beginning with
;; "?", and press C-c C-c.  The package submits the question to
;; `zeta-block-rpc-command', registers an `emacs_read' read-only tool for the
;; current live buffer, and inserts Zeta's answer below the block.

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

(defvar zeta-block--process nil)
(defvar zeta-block--next-id 1)
(defvar zeta-block--callbacks (make-hash-table :test 'eql))
(defvar zeta-block--active-buffer nil)
(defvar zeta-block--active-requests 0)
(defvar zeta-block--status 'off)
(defvar zeta-block--last-error nil)

(defvar zeta-block-mode-map
  (let ((map (make-sparse-keymap)))
    (define-key map (kbd "C-c C-c") #'zeta-block-submit)
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
  (pcase-let ((`(,_begin ,end ,raw) (zeta-block-current-block)))
    (let* ((comment-prefix (zeta-block-comment-prefix raw))
           (question (zeta-block-clean-question raw)))
      (if (not question)
          (zeta-block-dispatch-original-c-c-c)
        (let ((placeholder (zeta-block-insert-placeholder end comment-prefix)))
          (setq zeta-block--active-buffer (current-buffer))
          (zeta-block-ask-async
           question
           (lambda (result error)
             (zeta-block-replace-placeholder
              placeholder
              (zeta-block-response-text result error)
              comment-prefix))))))))

(defun zeta-block-dispatch-original-c-c-c ()
  "Call the command normally bound to `C-c C-c'."
  (let* ((zeta-block-mode nil)
         (command (key-binding (kbd "C-c C-c"))))
    (if (and command (not (eq command #'zeta-block-submit)))
        (call-interactively command)
      (user-error "Current block does not start with ?"))))

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

(defun zeta-block-replace-placeholder (markers text comment-prefix)
  "Replace MARKERS with TEXT formatted using COMMENT-PREFIX."
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
                  (insert (zeta-block-format-response text comment-prefix)))
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
   ((and (hash-table-p result) (gethash "final_text" result))
    (gethash "final_text" result))
   ((and (listp result) (alist-get 'final_text result))
    (alist-get 'final_text result))
   (t "No final answer.")))

(defun zeta-block-ask-async (question callback)
  "Ask Zeta QUESTION and call CALLBACK with (RESULT ERROR)."
  (zeta-block-ensure-process)
  (zeta-block-send-request
   "tools.register"
   `(("tools" . [,(zeta-block-emacs-read-tool)]))
   nil)
  (zeta-block-send-request
   "session.run"
   `(("workflow" . "ask")
     ("objective" . ,question)
     ("tools" . ,(vconcat zeta-block-read-only-tools))
     ("system" . ,(zeta-block-ask-system-prompt)))
   callback
   t))

(defun zeta-block-ask-system-prompt ()
  "Return the read-only system prompt used by `?' blocks."
  (string-join
   '("Answer concisely from the available project and editor context."
     "Use only read-only tools."
     "Use emacs_read when the live current buffer is relevant, because it can include unsaved edits."
     "Do not propose shell commands or mutations.")
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
    (zeta-block-send-request
     "tools.register"
     `(("tools" . [,(zeta-block-emacs-read-tool)]))
     nil)))

;;;###autoload
(defun zeta-block-restart ()
  "Restart the Zeta JSON-RPC subprocess used by `zeta-block-mode'."
  (interactive)
  (when (process-live-p zeta-block--process)
    (delete-process zeta-block--process))
  (setq zeta-block--process nil)
  (clrhash zeta-block--callbacks)
  (setq zeta-block--active-requests 0
        zeta-block--status 'off
        zeta-block--last-error nil)
  (zeta-block-ensure-process)
  (message "zeta-block restarted"))

(defun zeta-block-emacs-read-tool ()
  "Return the JSON-RPC descriptor for the Emacs read tool."
  '(("name" . "emacs_read")
    ("description" . "Read the current live Emacs buffer, including unsaved edits.")
    ("schema" . (("type" . "object")
                 ("additionalProperties" . :json-false)
                 ("properties" . (("start_line" . (("type" . "integer")
                                                   ("minimum" . 1)))
                                  ("end_line" . (("type" . "integer")
                                                 ("minimum" . 1)))))))))

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
        (when track
          (zeta-block-clear-running))
        (funcall callback (gethash "result" message) nil)))))

(defun zeta-block-handle-tool-call (params)
  "Handle a server tool call with PARAMS."
  (let* ((id (and (hash-table-p params) (gethash "id" params)))
         (name (and (hash-table-p params) (gethash "name" params)))
         (arguments (and (hash-table-p params) (gethash "arguments" params)))
         (result
          (if (string= name "emacs_read")
              (zeta-block-emacs-read arguments)
            `(("ok" . :json-false)
              ("error" . (("code" . "unknown-tool")
                          ("message" . ,(format "unknown Emacs tool: %s" name))))))))
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
               (text (buffer-substring-no-properties (car range) (cdr range)))
               (path (or buffer-file-name ""))
               (hash (secure-hash 'sha256 (buffer-substring-no-properties
                                           (point-min)
                                           (point-max)))))
          `(("ok" . t)
            ("content" . [,(list (cons "type" "text") (cons "text" text))])
            ("metadata" . (("buffer_name" . ,(buffer-name))
                           ("path" . ,path)
                           ("modified" . ,(if (buffer-modified-p) t :json-false))
                           ("modified_tick" . ,(buffer-chars-modified-tick))
                           ("content_hash" . ,(concat "sha256:" hash))
                           ("start_line" . ,(line-number-at-pos (car range)))
                           ("end_line" . ,(line-number-at-pos (cdr range)))))))))))

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
    (clrhash zeta-block--callbacks)
    (setq zeta-block--active-requests 0
          zeta-block--status (if (string-match-p "finished" event) 'off 'error)
          zeta-block--last-error (unless (eq zeta-block--status 'off)
                                   (string-trim (format "Zeta process %s" event))))
    (force-mode-line-update t)
    (when (eq process zeta-block--process)
      (setq zeta-block--process nil))))

(provide 'zeta-block)

;;; zeta-block.el ends here
