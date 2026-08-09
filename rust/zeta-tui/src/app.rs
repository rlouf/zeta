use std::collections::{HashMap, HashSet};
use std::io;

use crossterm::event::{
    DisableBracketedPaste, EnableBracketedPaste, Event as TerminalEvent, KeyCode, KeyEventKind,
    KeyModifiers, KeyboardEnhancementFlags, PopKeyboardEnhancementFlags,
    PushKeyboardEnhancementFlags,
};
use crossterm::execute;
use crossterm::terminal::{
    EnterAlternateScreen, LeaveAlternateScreen, disable_raw_mode, enable_raw_mode,
    supports_keyboard_enhancement,
};
use ratatui::Frame;
use ratatui::Terminal;
use ratatui::backend::CrosstermBackend;
use ratatui::layout::{Alignment, Constraint, Layout, Margin, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, List, ListItem, ListState, Paragraph};
use serde_json::Value;
use unicode_width::UnicodeWidthChar;

use crate::wire::{Cursor, Event, Session};

pub(super) struct App {
    protocol: String,
    sessions: Vec<Session>,
    selected_session: Option<usize>,
    events: Vec<Event>,
    cursor: Option<Cursor>,
    view: View,
    mode: Mode,
    session_views: HashMap<String, SessionViewState>,
    new_session_view: SessionViewState,
    keyboard_enhancement: bool,
    animation_frame: usize,
    submissions: Vec<Submission>,
}

#[derive(Debug, PartialEq, Eq)]
enum View {
    Sessions,
    Attached(String),
}

#[derive(Debug, PartialEq, Eq)]
enum Mode {
    Browse,
    Compose,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum TimelineMode {
    Semantic,
    Raw,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum TimelinePosition {
    Follow,
    Offset(usize),
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
struct Draft {
    text: String,
    cursor: usize,
}

#[derive(Debug, PartialEq, Eq)]
struct DraftLayout {
    lines: Vec<String>,
    cursor_row: usize,
    cursor_column: usize,
}

#[derive(Debug, PartialEq, Eq)]
struct SessionViewState {
    draft: Draft,
    timeline_mode: TimelineMode,
    timeline_position: TimelinePosition,
    timeline_max_offset: usize,
    timeline_viewport_height: usize,
    timeline_unseen_rows: usize,
    timeline_changed: bool,
    submitted_history: Vec<String>,
    history_position: Option<usize>,
    history_draft: Option<Draft>,
    yank: String,
}

impl Default for SessionViewState {
    fn default() -> Self {
        Self {
            draft: Draft::default(),
            timeline_mode: TimelineMode::Semantic,
            timeline_position: TimelinePosition::Follow,
            timeline_max_offset: 0,
            timeline_viewport_height: 1,
            timeline_unseen_rows: 0,
            timeline_changed: false,
            submitted_history: Vec::new(),
            history_position: None,
            history_draft: None,
            yank: String::new(),
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
struct SubmissionId(String);

#[derive(Clone, Debug, PartialEq, Eq)]
enum SubmissionTarget {
    NewSession,
    Session(String),
}

#[derive(Clone, Debug, PartialEq, Eq)]
enum SubmissionState {
    Sending,
    Queued,
    Failed(String),
}

#[derive(Clone, Debug, PartialEq, Eq)]
struct Submission {
    id: SubmissionId,
    target: SubmissionTarget,
    message: String,
    state: SubmissionState,
    event_id: Option<String>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
enum ToolOutcome {
    Completed { timestamp_ms: i64 },
    Failed { message: String, timestamp_ms: i64 },
}

#[derive(Debug, PartialEq, Eq)]
enum TimelineItem {
    User(String),
    AgentHeading,
    Agent(String),
    Activity {
        glyph: String,
        text: String,
        color: Color,
    },
    Raw {
        event_type: String,
        payload: String,
    },
}

#[derive(Debug, PartialEq, Eq)]
pub(super) enum AppAction {
    None,
    Quit,
    Submit(String),
}

pub(super) struct TerminalSession {
    terminal: Terminal<CrosstermBackend<io::Stdout>>,
    active: bool,
    keyboard_enhancement: bool,
}

impl Draft {
    fn clear(&mut self) {
        self.text.clear();
        self.cursor = 0;
    }

    fn insert(&mut self, text: &str) {
        self.text.insert_str(self.cursor, text);
        self.cursor += text.len();
    }

    fn replace(&mut self, text: String) {
        self.cursor = text.len();
        self.text = text;
    }

    fn insert_char(&mut self, character: char) {
        self.text.insert(self.cursor, character);
        self.cursor += character.len_utf8();
    }

    fn move_left(&mut self) {
        self.cursor = self.previous_boundary();
    }

    fn move_right(&mut self) {
        let Some(character) = self.text[self.cursor..].chars().next() else {
            return;
        };
        self.cursor += character.len_utf8();
    }

    fn move_to_start(&mut self) {
        self.cursor = 0;
    }

    fn move_to_end(&mut self) {
        self.cursor = self.text.len();
    }

    fn delete_backward(&mut self) {
        let start = self.previous_boundary();
        self.text.replace_range(start..self.cursor, "");
        self.cursor = start;
    }

    fn delete_forward(&mut self) {
        let Some(character) = self.text[self.cursor..].chars().next() else {
            return;
        };
        let end = self.cursor + character.len_utf8();
        self.text.replace_range(self.cursor..end, "");
    }

    fn delete_word_backward(&mut self) {
        let end = self.cursor;
        let mut start = end;
        while let Some((index, character)) = self.text[..start].char_indices().next_back() {
            if !character.is_whitespace() {
                break;
            }
            start = index;
        }
        while let Some((index, character)) = self.text[..start].char_indices().next_back() {
            if character.is_whitespace() {
                break;
            }
            start = index;
        }
        while let Some((index, character)) = self.text[..start].char_indices().next_back() {
            if character == '\n' || !character.is_whitespace() {
                break;
            }
            start = index;
        }
        self.text.replace_range(start..end, "");
        self.cursor = start;
    }

    fn delete_to_end(&mut self) -> String {
        let deleted = self.text[self.cursor..].to_owned();
        self.text.truncate(self.cursor);
        deleted
    }

    fn delete_to_start(&mut self) -> String {
        let deleted = self.text[..self.cursor].to_owned();
        self.text.replace_range(..self.cursor, "");
        self.cursor = 0;
        deleted
    }

    fn trimmed(&self) -> &str {
        self.text.trim()
    }

    fn is_empty(&self) -> bool {
        self.text.is_empty()
    }

    fn layout(&self, width: usize) -> DraftLayout {
        let width = width.max(1);
        let mut lines = vec![String::new()];
        let mut row = 0;
        let mut column = 0;
        let mut cursor_row = 0;
        let mut cursor_column = 0;

        for (index, character) in self.text.char_indices() {
            if index == self.cursor {
                cursor_row = row;
                cursor_column = column;
            }
            if character == '\n' {
                lines.push(String::new());
                row += 1;
                column = 0;
                continue;
            }
            let character_width = character.width().unwrap_or(0);
            if column > 0 && column + character_width > width {
                lines.push(String::new());
                row += 1;
                column = 0;
                if index == self.cursor {
                    cursor_row = row;
                    cursor_column = column;
                }
            }
            lines[row].push(character);
            column += character_width;
        }
        if self.cursor == self.text.len() {
            if column >= width {
                lines.push(String::new());
                cursor_row = row + 1;
                cursor_column = 0;
            } else {
                cursor_row = row;
                cursor_column = column;
            }
        }

        DraftLayout {
            lines,
            cursor_row,
            cursor_column,
        }
    }

    fn previous_boundary(&self) -> usize {
        let Some((index, _)) = self.text[..self.cursor].char_indices().next_back() else {
            return 0;
        };
        index
    }
}

impl SessionViewState {
    fn finish_history_navigation(&mut self) {
        self.history_position = None;
        self.history_draft = None;
    }

    fn remember_submission(&mut self, message: String) {
        if self.submitted_history.last() != Some(&message) {
            self.submitted_history.push(message);
        }
        self.finish_history_navigation();
    }

    fn previous_submission(&mut self) {
        if self.submitted_history.is_empty() {
            return;
        }
        let position = match self.history_position {
            Some(position) => position.saturating_sub(1),
            None => {
                self.history_draft = Some(self.draft.clone());
                self.submitted_history.len() - 1
            }
        };
        self.history_position = Some(position);
        self.draft.replace(self.submitted_history[position].clone());
    }

    fn next_submission(&mut self) {
        let Some(position) = self.history_position else {
            return;
        };
        if position + 1 < self.submitted_history.len() {
            let position = position + 1;
            self.history_position = Some(position);
            self.draft.replace(self.submitted_history[position].clone());
            return;
        }
        let draft = self.history_draft.take().unwrap_or_default();
        self.draft = draft;
        self.history_position = None;
    }
}

impl App {
    pub(super) fn connected(
        protocol: String,
        sessions: Vec<Session>,
        events: Vec<Event>,
        cursor: Option<Cursor>,
    ) -> Self {
        let selected_session = if sessions.is_empty() { None } else { Some(0) };
        let mut session_views = HashMap::new();
        for session in &sessions {
            session_views.insert(session.session_id().to_owned(), SessionViewState::default());
        }
        Self {
            protocol,
            sessions,
            selected_session,
            events,
            cursor,
            view: View::Sessions,
            mode: Mode::Browse,
            session_views,
            new_session_view: SessionViewState::default(),
            keyboard_enhancement: false,
            animation_frame: 0,
            submissions: Vec::new(),
        }
    }

    pub(super) fn handle_event(&mut self, event: &TerminalEvent) -> AppAction {
        if self.mode == Mode::Compose
            && let TerminalEvent::Paste(text) = event
        {
            let state = self.view_state_mut();
            state.finish_history_navigation();
            state.draft.insert(text);
            return AppAction::None;
        }
        let TerminalEvent::Key(key) = event else {
            return AppAction::None;
        };
        if key.kind != KeyEventKind::Press {
            return AppAction::None;
        }

        match self.mode {
            Mode::Browse => {
                if key.code == KeyCode::Char('q') {
                    return AppAction::Quit;
                }
                match &self.view {
                    View::Sessions => {
                        if key.code == KeyCode::Down || key.code == KeyCode::Char('j') {
                            self.select_next_session();
                            return AppAction::None;
                        }
                        if key.code == KeyCode::Up || key.code == KeyCode::Char('k') {
                            self.select_previous_session();
                            return AppAction::None;
                        }
                        if key.code == KeyCode::Char('n') {
                            self.mode = Mode::Compose;
                            return AppAction::None;
                        }
                        if key.code == KeyCode::Enter {
                            let Some(session_id) = self.selected_session_id() else {
                                return AppAction::None;
                            };
                            self.attach_session(session_id.to_owned());
                        }
                    }
                    View::Attached(_) => {
                        if key.code == KeyCode::Esc {
                            self.view = View::Sessions;
                            return AppAction::None;
                        }
                        if key.code == KeyCode::Char('i') {
                            self.mode = Mode::Compose;
                            return AppAction::None;
                        }
                        if key.code == KeyCode::Char('v') {
                            let timeline_mode = match self.view_state().timeline_mode {
                                TimelineMode::Semantic => TimelineMode::Raw,
                                TimelineMode::Raw => TimelineMode::Semantic,
                            };
                            self.view_state_mut().timeline_mode = timeline_mode;
                            self.follow_timeline();
                            return AppAction::None;
                        }
                        if key.code == KeyCode::Char('g') {
                            self.view_state_mut().timeline_position = TimelinePosition::Offset(0);
                            return AppAction::None;
                        }
                        if key.code == KeyCode::Char('G') {
                            self.follow_timeline();
                            return AppAction::None;
                        }
                        if key.code == KeyCode::PageUp {
                            self.scroll_timeline_page_up();
                            return AppAction::None;
                        }
                        if key.code == KeyCode::PageDown {
                            self.scroll_timeline_page_down();
                            return AppAction::None;
                        }
                        if key.code == KeyCode::Down || key.code == KeyCode::Char('j') {
                            self.scroll_timeline_down();
                            return AppAction::None;
                        }
                        if key.code == KeyCode::Up || key.code == KeyCode::Char('k') {
                            self.scroll_timeline_up();
                        }
                    }
                }
                AppAction::None
            }
            Mode::Compose => {
                if key.code == KeyCode::Esc {
                    self.mode = Mode::Browse;
                    return AppAction::None;
                }
                if key.code == KeyCode::Enter && key.modifiers.contains(KeyModifiers::SHIFT) {
                    let state = self.view_state_mut();
                    state.finish_history_navigation();
                    state.draft.insert("\n");
                    return AppAction::None;
                }
                if key.code == KeyCode::Char('j') && key.modifiers.contains(KeyModifiers::CONTROL) {
                    let state = self.view_state_mut();
                    state.finish_history_navigation();
                    state.draft.insert("\n");
                    return AppAction::None;
                }
                if key.code == KeyCode::Enter {
                    let message = self.view_state().draft.trimmed();
                    if message.is_empty() {
                        return AppAction::None;
                    }
                    let message = message.to_owned();
                    let state = self.view_state_mut();
                    state.remember_submission(message.clone());
                    state.draft.clear();
                    self.mode = Mode::Browse;
                    return AppAction::Submit(message);
                }
                if key.code == KeyCode::Char('a') && key.modifiers.contains(KeyModifiers::CONTROL) {
                    self.view_state_mut().draft.move_to_start();
                    return AppAction::None;
                }
                if key.code == KeyCode::Char('e') && key.modifiers.contains(KeyModifiers::CONTROL) {
                    self.view_state_mut().draft.move_to_end();
                    return AppAction::None;
                }
                if key.code == KeyCode::Char('k') && key.modifiers.contains(KeyModifiers::CONTROL) {
                    let state = self.view_state_mut();
                    state.finish_history_navigation();
                    state.yank = state.draft.delete_to_end();
                    return AppAction::None;
                }
                if key.code == KeyCode::Char('u') && key.modifiers.contains(KeyModifiers::CONTROL) {
                    let state = self.view_state_mut();
                    state.finish_history_navigation();
                    state.yank = state.draft.delete_to_start();
                    return AppAction::None;
                }
                if key.code == KeyCode::Char('y') && key.modifiers.contains(KeyModifiers::CONTROL) {
                    let state = self.view_state_mut();
                    state.finish_history_navigation();
                    let yank = state.yank.clone();
                    state.draft.insert(&yank);
                    return AppAction::None;
                }
                if key.code == KeyCode::Char('p') && key.modifiers.contains(KeyModifiers::CONTROL) {
                    self.view_state_mut().previous_submission();
                    return AppAction::None;
                }
                if key.code == KeyCode::Char('n') && key.modifiers.contains(KeyModifiers::CONTROL) {
                    self.view_state_mut().next_submission();
                    return AppAction::None;
                }
                if key.code == KeyCode::Left {
                    self.view_state_mut().draft.move_left();
                    return AppAction::None;
                }
                if key.code == KeyCode::Right {
                    self.view_state_mut().draft.move_right();
                    return AppAction::None;
                }
                if key.code == KeyCode::Home {
                    self.view_state_mut().draft.move_to_start();
                    return AppAction::None;
                }
                if key.code == KeyCode::End {
                    self.view_state_mut().draft.move_to_end();
                    return AppAction::None;
                }
                if key.code == KeyCode::Backspace {
                    self.view_state_mut().finish_history_navigation();
                    if key.modifiers.contains(KeyModifiers::ALT) {
                        self.view_state_mut().draft.delete_word_backward();
                    } else {
                        self.view_state_mut().draft.delete_backward();
                    }
                    return AppAction::None;
                }
                if key.code == KeyCode::Delete {
                    self.view_state_mut().finish_history_navigation();
                    self.view_state_mut().draft.delete_forward();
                    return AppAction::None;
                }
                if key.code == KeyCode::Char('w') && key.modifiers.contains(KeyModifiers::CONTROL) {
                    let state = self.view_state_mut();
                    state.finish_history_navigation();
                    state.draft.delete_word_backward();
                    return AppAction::None;
                }
                if let KeyCode::Char(character) = key.code
                    && !key.modifiers.contains(KeyModifiers::CONTROL)
                    && !key.modifiers.contains(KeyModifiers::SUPER)
                {
                    let state = self.view_state_mut();
                    state.finish_history_navigation();
                    state.draft.insert_char(character);
                }
                AppAction::None
            }
        }
    }

    pub(super) fn cursor(&self) -> Option<u64> {
        if let Some(cursor) = self.cursor {
            return Some(cursor.0);
        }
        None
    }

    pub(super) fn advance_animation(&mut self) {
        self.animation_frame = (self.animation_frame + 1) % 4;
    }

    pub(super) fn set_keyboard_enhancement(&mut self, supported: bool) {
        self.keyboard_enhancement = supported;
    }

    pub(super) fn append_events(&mut self, events: Vec<Event>, cursor: Option<Cursor>) {
        for event in events {
            for (session_id, state) in &mut self.session_views {
                if event.belongs_to_session(session_id) {
                    state.timeline_changed = true;
                }
            }
            let event_id = event.id();
            let durable_submission_id = if event.is_direct_message_request() {
                event
                    .idempotency_key()
                    .and_then(|key| key.rsplit(':').next())
            } else {
                None
            };
            self.submissions.retain(|submission| {
                submission.event_id.as_deref() != Some(event_id)
                    && durable_submission_id != Some(&submission.id.0)
            });
            self.events.push(event);
        }
        self.cursor = cursor;
    }

    pub(super) fn submission_started(&mut self, id: String, message: String) {
        let target = match self.attached_session_id() {
            Some(session_id) => SubmissionTarget::Session(session_id.to_owned()),
            None => SubmissionTarget::NewSession,
        };
        self.submissions.retain(|submission| {
            let failed = match &submission.state {
                SubmissionState::Failed(_) => true,
                SubmissionState::Sending | SubmissionState::Queued => false,
            };
            submission.target != target || submission.message != message || !failed
        });
        self.submissions.push(Submission {
            id: SubmissionId(id),
            target,
            message,
            state: SubmissionState::Sending,
            event_id: None,
        });
        self.follow_timeline();
    }

    pub(super) fn submission_queued(&mut self, id: &str, event_id: &str, session_id: &str) {
        let event_exists = self.events.iter().any(|event| event.id() == event_id);
        let Some(submission) = self
            .submissions
            .iter_mut()
            .find(|submission| submission.id.0 == id)
        else {
            return;
        };
        let started_session = submission.target == SubmissionTarget::NewSession;
        submission.target = SubmissionTarget::Session(session_id.to_owned());
        submission.state = SubmissionState::Queued;
        submission.event_id = Some(event_id.to_owned());
        if event_exists {
            self.submissions
                .retain(|submission| submission.event_id.as_deref() != Some(event_id));
        }
        if started_session {
            self.attach_session(session_id.to_owned());
            self.follow_timeline();
        }
    }

    pub(super) fn submission_failed(&mut self, id: &str, error: &str) {
        let Some(submission) = self
            .submissions
            .iter_mut()
            .find(|submission| submission.id.0 == id)
        else {
            return;
        };
        submission.state = SubmissionState::Failed(single_line(error));
        let target = submission.target.clone();
        let message = submission.message.clone();
        match &target {
            SubmissionTarget::NewSession => self.new_session_view.draft.replace(message),
            SubmissionTarget::Session(session_id) => self
                .session_views
                .entry(session_id.to_owned())
                .or_default()
                .draft
                .replace(message),
        }
        if self.current_submission_target() == target {
            self.mode = Mode::Compose;
        }
    }

    #[allow(clippy::question_mark)]
    pub(super) fn selected_session_id(&self) -> Option<&str> {
        let Some(index) = self.selected_session else {
            return None;
        };
        let Some(session) = self.sessions.get(index) else {
            return None;
        };
        Some(session.session_id())
    }

    pub(super) fn attached_session_id(&self) -> Option<&str> {
        match &self.view {
            View::Sessions => None,
            View::Attached(session_id) => Some(session_id),
        }
    }

    fn current_submission_target(&self) -> SubmissionTarget {
        match &self.view {
            View::Sessions => SubmissionTarget::NewSession,
            View::Attached(session_id) => SubmissionTarget::Session(session_id.to_owned()),
        }
    }

    fn attach_session(&mut self, session_id: String) {
        self.session_views.entry(session_id.clone()).or_default();
        self.view = View::Attached(session_id);
    }

    fn view_state(&self) -> &SessionViewState {
        match &self.view {
            View::Sessions => &self.new_session_view,
            View::Attached(session_id) => self
                .session_views
                .get(session_id)
                .expect("attached sessions own viewer state"),
        }
    }

    fn view_state_mut(&mut self) -> &mut SessionViewState {
        match &self.view {
            View::Sessions => &mut self.new_session_view,
            View::Attached(session_id) => self
                .session_views
                .get_mut(session_id)
                .expect("attached sessions own viewer state"),
        }
    }

    #[allow(clippy::manual_map)]
    pub(super) fn replace_sessions(&mut self, sessions: Vec<Session>) {
        let selected_id = match self.selected_session_id() {
            Some(session_id) => Some(session_id.to_owned()),
            None => None,
        };
        self.sessions = sessions;
        for session in &self.sessions {
            self.session_views
                .entry(session.session_id().to_owned())
                .or_default();
        }
        self.selected_session = None;
        let Some(selected_id) = selected_id else {
            if !self.sessions.is_empty() {
                self.selected_session = Some(0);
            }
            return;
        };
        for (index, session) in self.sessions.iter().enumerate() {
            if session.session_id() == selected_id {
                self.selected_session = Some(index);
                return;
            }
        }
        if !self.sessions.is_empty() {
            self.selected_session = Some(0);
        }
    }

    fn timeline_events(&self) -> Vec<&Event> {
        let Some(session_id) = self.attached_session_id() else {
            return Vec::new();
        };
        let mut direct_messages = HashSet::new();
        for event in &self.events {
            if !event.belongs_to_session(session_id) {
                continue;
            }
            if !event.is_direct_message_request() {
                continue;
            }
            if let Some(key) = event.user_message_key() {
                direct_messages.insert(key);
            }
        }

        let mut runtime_messages = HashSet::new();
        let mut timeline = Vec::new();
        for event in &self.events {
            if !event.belongs_to_session(session_id) {
                continue;
            }
            if !event.is_runtime_user_message() {
                timeline.push(event);
                continue;
            }
            let Some(key) = event.user_message_key() else {
                timeline.push(event);
                continue;
            };
            if direct_messages.contains(&key) {
                continue;
            }
            if runtime_messages.insert(key) {
                timeline.push(event);
            }
        }
        timeline
    }

    fn session_title(&self, session_id: &str) -> String {
        let mut runtime_message = None;
        for event in &self.events {
            if !event.belongs_to_session(session_id) {
                continue;
            }
            if event.is_direct_message_request() {
                let Some(message) = event.payload().get("message").and_then(Value::as_str) else {
                    continue;
                };
                return single_line(message);
            }
            if !event.is_runtime_user_message() || runtime_message.is_some() {
                continue;
            }
            let Some(message) = event.payload().get("content").and_then(Value::as_str) else {
                continue;
            };
            runtime_message = Some(single_line(message));
        }
        match runtime_message {
            Some(message) => message,
            None => format!("Session {}", abbreviated_id(session_id)),
        }
    }

    #[allow(clippy::manual_find)]
    fn attached_session(&self) -> Option<&Session> {
        let session_id = self.attached_session_id()?;
        for session in &self.sessions {
            if session.session_id() == session_id {
                return Some(session);
            }
        }
        None
    }

    fn attached_title(&self) -> String {
        let Some(session_id) = self.attached_session_id() else {
            return "Sessions".to_owned();
        };
        self.session_title(session_id)
    }

    fn timeline_items(&self) -> Vec<TimelineItem> {
        let events = self.timeline_events();
        let timeline_mode = self.view_state().timeline_mode;
        let mut items = match timeline_mode {
            TimelineMode::Raw => raw_timeline_items(&events),
            TimelineMode::Semantic => semantic_timeline_items(&events, self.animation_frame),
        };
        if timeline_mode == TimelineMode::Raw {
            return items;
        }
        let Some(session_id) = self.attached_session_id() else {
            return items;
        };
        for submission in &self.submissions {
            let SubmissionTarget::Session(target_session_id) = &submission.target else {
                continue;
            };
            if target_session_id != session_id {
                continue;
            }
            items.push(TimelineItem::User(submission.message.clone()));
            match &submission.state {
                SubmissionState::Sending => {
                    push_activity(&mut items, "·", "Sending…".to_owned(), Color::Yellow)
                }
                SubmissionState::Queued => {
                    push_activity(&mut items, "·", "Queued".to_owned(), Color::Yellow);
                }
                SubmissionState::Failed(error) => {
                    push_activity(&mut items, "×", format!("Failed — {error}"), Color::Red)
                }
            }
        }
        items
    }

    fn new_session_submission(&self) -> Option<&Submission> {
        self.submissions
            .iter()
            .rev()
            .find(|submission| submission.target == SubmissionTarget::NewSession)
    }

    fn session_activity(&self, session_id: &str) -> Option<String> {
        let mut events = Vec::new();
        for event in &self.events {
            if event.belongs_to_session(session_id) {
                events.push(event);
            }
        }
        let items = semantic_timeline_items(&events, self.animation_frame);
        for item in items.into_iter().rev() {
            match item {
                TimelineItem::Agent(content) => return Some(single_line(&content)),
                TimelineItem::Activity { text, .. } => return Some(text),
                TimelineItem::User(_) | TimelineItem::AgentHeading | TimelineItem::Raw { .. } => {}
            }
        }
        None
    }

    fn timeline_offset(&mut self, row_count: usize, viewport_height: usize) -> usize {
        let state = self.view_state_mut();
        let previous_max_offset = state.timeline_max_offset;
        state.timeline_viewport_height = viewport_height.max(1);
        state.timeline_max_offset = row_count.saturating_sub(viewport_height);
        if state.timeline_changed {
            if let TimelinePosition::Offset(_) = state.timeline_position {
                state.timeline_unseen_rows += state
                    .timeline_max_offset
                    .saturating_sub(previous_max_offset);
            }
            state.timeline_changed = false;
        }
        match state.timeline_position {
            TimelinePosition::Follow => {
                state.timeline_unseen_rows = 0;
                state.timeline_max_offset
            }
            TimelinePosition::Offset(offset) => {
                let offset = offset.min(state.timeline_max_offset);
                state.timeline_position = TimelinePosition::Offset(offset);
                offset
            }
        }
    }

    fn scroll_timeline_up(&mut self) {
        let state = self.view_state_mut();
        let offset = match state.timeline_position {
            TimelinePosition::Follow => state.timeline_max_offset,
            TimelinePosition::Offset(offset) => offset,
        };
        state.timeline_position = TimelinePosition::Offset(offset.saturating_sub(1));
    }

    fn scroll_timeline_down(&mut self) {
        let state = self.view_state_mut();
        match state.timeline_position {
            TimelinePosition::Follow => {}
            TimelinePosition::Offset(offset) => {
                if offset >= state.timeline_max_offset.saturating_sub(1) {
                    state.timeline_position = TimelinePosition::Follow;
                    state.timeline_unseen_rows = 0;
                    state.timeline_changed = false;
                } else {
                    state.timeline_position = TimelinePosition::Offset(offset + 1);
                }
            }
        }
    }

    fn scroll_timeline_page_up(&mut self) {
        let state = self.view_state_mut();
        let offset = match state.timeline_position {
            TimelinePosition::Follow => state.timeline_max_offset,
            TimelinePosition::Offset(offset) => offset,
        };
        state.timeline_position =
            TimelinePosition::Offset(offset.saturating_sub(state.timeline_viewport_height));
    }

    fn scroll_timeline_page_down(&mut self) {
        let state = self.view_state_mut();
        let TimelinePosition::Offset(offset) = state.timeline_position else {
            return;
        };
        let offset = offset.saturating_add(state.timeline_viewport_height);
        if offset >= state.timeline_max_offset {
            state.timeline_position = TimelinePosition::Follow;
            state.timeline_unseen_rows = 0;
            state.timeline_changed = false;
        } else {
            state.timeline_position = TimelinePosition::Offset(offset);
        }
    }

    fn follow_timeline(&mut self) {
        let state = self.view_state_mut();
        state.timeline_position = TimelinePosition::Follow;
        state.timeline_unseen_rows = 0;
        state.timeline_changed = false;
    }

    fn shows_composer(&self) -> bool {
        match (&self.view, &self.mode) {
            (View::Sessions, Mode::Browse) => false,
            (View::Sessions, Mode::Compose)
            | (View::Attached(_), Mode::Browse)
            | (View::Attached(_), Mode::Compose) => true,
        }
    }

    fn composer_height(&self, width: u16) -> u16 {
        if !self.shows_composer() {
            return 0;
        }
        if self.mode == Mode::Browse || self.view_state().draft.is_empty() {
            return 2;
        }
        let input_width = usize::from(width.saturating_sub(2));
        let line_count = self
            .view_state()
            .draft
            .layout(input_width)
            .lines
            .len()
            .clamp(1, 8);
        u16::try_from(line_count + 1).unwrap_or(9)
    }

    fn select_next_session(&mut self) {
        if self.sessions.is_empty() {
            self.selected_session = None;
            return;
        }
        let Some(index) = self.selected_session else {
            self.selected_session = Some(0);
            return;
        };
        if index + 1 < self.sessions.len() {
            self.selected_session = Some(index + 1);
        }
    }

    fn select_previous_session(&mut self) {
        if self.sessions.is_empty() {
            self.selected_session = None;
            return;
        }
        let Some(index) = self.selected_session else {
            self.selected_session = Some(0);
            return;
        };
        self.selected_session = Some(index.saturating_sub(1));
    }
}

fn raw_timeline_items(events: &[&Event]) -> Vec<TimelineItem> {
    let mut items = Vec::new();
    for event in events {
        items.push(TimelineItem::Raw {
            event_type: event.event_type().to_owned(),
            payload: event.timeline_text(),
        });
    }
    items
}

fn semantic_timeline_items(events: &[&Event], animation_frame: usize) -> Vec<TimelineItem> {
    let outcomes = tool_outcomes(events);
    let completed_runs = completed_model_runs(events);
    let terminal_runs = terminal_runs(events);
    let mut seen_tool_calls = HashSet::new();
    let mut items = Vec::new();
    let mut agent_started = false;

    for event in events {
        let event_type = event.event_type();
        if event.is_direct_message_request() {
            if let Some(message) = payload_string(event, "message") {
                items.push(TimelineItem::User(message.to_owned()));
                agent_started = false;
            }
            continue;
        }
        if event.is_runtime_user_message() {
            if let Some(message) = payload_string(event, "content") {
                items.push(TimelineItem::User(message.to_owned()));
                agent_started = false;
            }
            continue;
        }
        if event_type == "runtime.stream.chunk" {
            let completed = match event.run_id() {
                Some(run_id) => completed_runs.contains(run_id),
                None => false,
            };
            if completed {
                continue;
            }
            if let Some(text) = payload_string(event, "text") {
                start_agent_timeline(&mut items, &mut agent_started);
                append_agent_text(&mut items, text);
            }
            continue;
        }
        if event_type == "runtime.status.update" {
            let status = payload_string(event, "status").unwrap_or("working");
            let text = if status == "reasoning_delta" {
                "Thinking".to_owned()
            } else {
                match payload_string(event, "text") {
                    Some(text) => single_line(text),
                    None => humanize(status),
                }
            };
            start_agent_timeline(&mut items, &mut agent_started);
            let active = status == "reasoning_delta"
                && match event.run_id() {
                    Some(run_id) => !terminal_runs.contains(run_id),
                    None => true,
                };
            let glyph = if active {
                active_glyph(animation_frame)
            } else {
                "·"
            };
            push_activity(&mut items, glyph, text, Color::DarkGray);
            continue;
        }
        if event_type == "zeta.model_call.completed" {
            let Some(content) = payload_string(event, "content") else {
                continue;
            };
            if !content.trim().is_empty() {
                start_agent_timeline(&mut items, &mut agent_started);
                items.push(TimelineItem::Agent(content.to_owned()));
            }
            continue;
        }
        if event_type == "zeta.tool_call.started" {
            let description = tool_description(event);
            start_agent_timeline(&mut items, &mut agent_started);
            let Some(tool_call_id) = payload_string(event, "tool_call_id") else {
                push_activity(&mut items, "↳", description, Color::DarkGray);
                continue;
            };
            seen_tool_calls.insert(tool_call_id.to_owned());
            match outcomes.get(tool_call_id) {
                Some(ToolOutcome::Completed { timestamp_ms }) => {
                    let text = with_duration(description, event.timestamp_ms(), *timestamp_ms);
                    push_activity(&mut items, "✓", text, Color::Green);
                }
                Some(ToolOutcome::Failed {
                    message,
                    timestamp_ms,
                }) => {
                    let text = with_duration(
                        format!("{description} failed: {message}"),
                        event.timestamp_ms(),
                        *timestamp_ms,
                    );
                    push_activity(&mut items, "×", text, Color::Red);
                }
                None => push_activity(
                    &mut items,
                    active_glyph(animation_frame),
                    description,
                    Color::DarkGray,
                ),
            }
            continue;
        }
        if event_type == "zeta.tool_call.completed" || event_type == "zeta.tool_call.failed" {
            let tool_call_id = payload_string(event, "tool_call_id");
            let already_rendered = match tool_call_id {
                Some(tool_call_id) => seen_tool_calls.contains(tool_call_id),
                None => false,
            };
            if already_rendered {
                continue;
            }
            let description = tool_description(event);
            start_agent_timeline(&mut items, &mut agent_started);
            if event_type == "zeta.tool_call.failed" {
                let message = event_error(event).unwrap_or_else(|| "unknown error".to_owned());
                push_activity(
                    &mut items,
                    "×",
                    format!("{description} failed: {message}"),
                    Color::Red,
                );
            } else {
                push_activity(&mut items, "✓", description, Color::Green);
            }
            continue;
        }
        if event_type == "zeta.turn.completed" {
            start_agent_timeline(&mut items, &mut agent_started);
            push_activity(&mut items, "✓", "Completed".to_owned(), Color::Green);
            continue;
        }
        if event_type == "zeta.turn.failed" {
            let message = payload_string(event, "content")
                .or_else(|| payload_string(event, "reason"))
                .unwrap_or("Turn failed");
            start_agent_timeline(&mut items, &mut agent_started);
            push_activity(&mut items, "×", single_line(message), Color::Red);
            continue;
        }
        if event_type.starts_with("runtime.")
            || event_type.starts_with("session.")
            || event_type.starts_with("zeta.")
            || event_type.starts_with("rpc.")
        {
            continue;
        }
        start_agent_timeline(&mut items, &mut agent_started);
        push_activity(&mut items, "•", humanize(event_type), Color::DarkGray);
    }
    items
}

fn start_agent_timeline(items: &mut Vec<TimelineItem>, agent_started: &mut bool) {
    if *agent_started {
        return;
    }
    items.push(TimelineItem::AgentHeading);
    *agent_started = true;
}

fn completed_model_runs(events: &[&Event]) -> HashSet<String> {
    let mut runs = HashSet::new();
    for event in events {
        if event.event_type() != "zeta.model_call.completed" {
            continue;
        }
        let Some(content) = payload_string(event, "content") else {
            continue;
        };
        if content.trim().is_empty() {
            continue;
        }
        if let Some(run_id) = event.run_id() {
            runs.insert(run_id.to_owned());
        }
    }
    runs
}

fn terminal_runs(events: &[&Event]) -> HashSet<String> {
    let mut runs = HashSet::new();
    for event in events {
        if event.event_type() != "zeta.turn.completed" && event.event_type() != "zeta.turn.failed" {
            continue;
        }
        if let Some(run_id) = event.run_id() {
            runs.insert(run_id.to_owned());
        }
    }
    runs
}

fn tool_outcomes(events: &[&Event]) -> HashMap<String, ToolOutcome> {
    let mut outcomes = HashMap::new();
    for event in events {
        let event_type = event.event_type();
        if event_type != "zeta.tool_call.completed" && event_type != "zeta.tool_call.failed" {
            continue;
        }
        let Some(tool_call_id) = payload_string(event, "tool_call_id") else {
            continue;
        };
        let outcome = if event_type == "zeta.tool_call.failed" {
            let message = event_error(event).unwrap_or_else(|| "unknown error".to_owned());
            ToolOutcome::Failed {
                message,
                timestamp_ms: event.timestamp_ms(),
            }
        } else {
            ToolOutcome::Completed {
                timestamp_ms: event.timestamp_ms(),
            }
        };
        outcomes.insert(tool_call_id.to_owned(), outcome);
    }
    outcomes
}

fn payload_string<'a>(event: &'a Event, key: &str) -> Option<&'a str> {
    event.payload().get(key).and_then(Value::as_str)
}

fn event_error(event: &Event) -> Option<String> {
    let result = event.payload().get("result")?;
    let message = result
        .get("error")
        .and_then(|error| error.get("message"))
        .and_then(Value::as_str);
    if let Some(message) = message {
        return Some(single_line(message));
    }
    let message = result
        .get("refusal")
        .and_then(|refusal| refusal.get("message"))
        .and_then(Value::as_str);
    if let Some(message) = message {
        return Some(single_line(message));
    }
    payload_string(event, "error").map(single_line)
}

fn tool_description(event: &Event) -> String {
    let name = payload_string(event, "name").unwrap_or("tool");
    let Some(input) = event.payload().get("input") else {
        return name.to_owned();
    };
    for key in ["path", "command", "query", "pattern", "url"] {
        let Some(value) = input.get(key).and_then(Value::as_str) else {
            continue;
        };
        let value = single_line(value);
        if !value.is_empty() {
            return format!("{name} {value}");
        }
    }
    name.to_owned()
}

fn append_agent_text(items: &mut Vec<TimelineItem>, text: &str) {
    match items.last_mut() {
        Some(TimelineItem::Agent(content)) => content.push_str(text),
        Some(TimelineItem::User(_))
        | Some(TimelineItem::AgentHeading)
        | Some(TimelineItem::Activity { .. })
        | Some(TimelineItem::Raw { .. })
        | None => items.push(TimelineItem::Agent(text.to_owned())),
    }
}

fn push_activity(items: &mut Vec<TimelineItem>, glyph: &str, text: String, color: Color) {
    items.push(TimelineItem::Activity {
        glyph: glyph.to_owned(),
        text,
        color,
    });
}

fn active_glyph(animation_frame: usize) -> &'static str {
    match animation_frame % 4 {
        0 => "·",
        1 => "∙",
        2 => "•",
        3 => "∙",
        _ => unreachable!(),
    }
}

fn with_duration(text: String, started_at_ms: i64, completed_at_ms: i64) -> String {
    let Some(duration_ms) = completed_at_ms.checked_sub(started_at_ms) else {
        return text;
    };
    if duration_ms < 0 {
        return text;
    }
    if duration_ms < 1_000 {
        return format!("{text} · {duration_ms}ms");
    }
    let seconds = duration_ms / 1_000;
    let milliseconds = duration_ms % 1_000;
    if milliseconds == 0 {
        return format!("{text} · {seconds}s");
    }
    let fractional = format!("{milliseconds:03}")
        .trim_end_matches('0')
        .to_owned();
    format!("{text} · {seconds}.{fractional}s")
}

fn single_line(text: &str) -> String {
    let mut output = String::new();
    for word in text.split_whitespace() {
        if !output.is_empty() {
            output.push(' ');
        }
        output.push_str(word);
    }
    output
}

fn humanize(value: &str) -> String {
    let mut output = String::new();
    for character in value.chars() {
        if character == '.' || character == '_' {
            output.push(' ');
        } else {
            output.push(character);
        }
    }
    output
}

fn abbreviated_id(value: &str) -> String {
    let mut output = String::new();
    for character in value.chars() {
        if output.chars().count() == 8 {
            break;
        }
        output.push(character);
    }
    output
}

impl TerminalSession {
    pub(super) fn start() -> io::Result<Self> {
        enable_raw_mode()?;
        let keyboard_enhancement = match supports_keyboard_enhancement() {
            Ok(supported) => supported,
            Err(error) => {
                drop(error);
                false
            }
        };
        let mut output = io::stdout();
        if let Err(error) = execute!(output, EnterAlternateScreen) {
            let restore_result = disable_raw_mode();
            if let Err(restore_error) = restore_result {
                return Err(io::Error::other(format!(
                    "{error}; raw-mode restoration failed: {restore_error}"
                )));
            }
            return Err(error);
        }
        if keyboard_enhancement
            && let Err(error) = execute!(
                output,
                PushKeyboardEnhancementFlags(
                    KeyboardEnhancementFlags::DISAMBIGUATE_ESCAPE_CODES
                        | KeyboardEnhancementFlags::REPORT_ALL_KEYS_AS_ESCAPE_CODES
                        | KeyboardEnhancementFlags::REPORT_ALTERNATE_KEYS
                        | KeyboardEnhancementFlags::REPORT_EVENT_TYPES
                )
            )
        {
            let _ = execute!(output, LeaveAlternateScreen);
            let _ = disable_raw_mode();
            return Err(error);
        }
        if let Err(error) = execute!(output, EnableBracketedPaste) {
            if keyboard_enhancement {
                let _ = execute!(output, PopKeyboardEnhancementFlags);
            }
            let _ = execute!(output, LeaveAlternateScreen);
            let _ = disable_raw_mode();
            return Err(error);
        }

        let backend = CrosstermBackend::new(output);
        let terminal = match Terminal::new(backend) {
            Ok(terminal) => terminal,
            Err(error) => {
                let raw_result = disable_raw_mode();
                let mut output = io::stdout();
                let feature_result = if keyboard_enhancement {
                    execute!(
                        output,
                        DisableBracketedPaste,
                        PopKeyboardEnhancementFlags,
                        LeaveAlternateScreen
                    )
                } else {
                    execute!(output, DisableBracketedPaste, LeaveAlternateScreen)
                };
                if let Err(restore_error) = raw_result {
                    return Err(io::Error::other(format!(
                        "{error}; raw-mode restoration failed: {restore_error}"
                    )));
                }
                if let Err(restore_error) = feature_result {
                    return Err(io::Error::other(format!(
                        "{error}; screen restoration failed: {restore_error}"
                    )));
                }
                return Err(error);
            }
        };
        Ok(Self {
            terminal,
            active: true,
            keyboard_enhancement,
        })
    }

    pub(super) fn keyboard_enhancement(&self) -> bool {
        self.keyboard_enhancement
    }

    pub(super) fn draw(&mut self, app: &mut App) -> io::Result<()> {
        self.terminal.draw(|frame| draw(frame, app))?;
        Ok(())
    }

    pub(super) fn restore(&mut self) -> io::Result<()> {
        if !self.active {
            return Ok(());
        }
        self.active = false;
        let raw_result = disable_raw_mode();
        let screen_result = if self.keyboard_enhancement {
            execute!(
                self.terminal.backend_mut(),
                DisableBracketedPaste,
                PopKeyboardEnhancementFlags,
                LeaveAlternateScreen
            )
        } else {
            execute!(
                self.terminal.backend_mut(),
                DisableBracketedPaste,
                LeaveAlternateScreen
            )
        };
        let cursor_result = self.terminal.show_cursor();
        raw_result?;
        screen_result?;
        cursor_result?;
        Ok(())
    }
}

impl Drop for TerminalSession {
    fn drop(&mut self) {
        let _ = self.restore();
    }
}

pub(super) fn draw(frame: &mut Frame<'_>, app: &mut App) {
    let area = padded_area(frame.area());
    let composer_height = app.composer_height(area.width);
    let areas = Layout::vertical([
        Constraint::Length(2),
        Constraint::Min(3),
        Constraint::Length(composer_height),
        Constraint::Length(1),
    ])
    .split(area);

    render_header(frame, areas[0], app);

    match &app.view {
        View::Sessions => render_sessions(frame, areas[1], app),
        View::Attached(_) => render_timeline(frame, areas[1], app),
    }

    if app.shows_composer() {
        render_composer(frame, areas[2], app);
    }

    frame.render_widget(Paragraph::new(footer_line(app)), areas[3]);
}

fn render_sessions(frame: &mut Frame<'_>, area: Rect, app: &App) {
    let areas = Layout::vertical([Constraint::Length(2), Constraint::Min(1)]).split(area);
    frame.render_widget(
        Paragraph::new("Sessions").style(Style::default().add_modifier(Modifier::BOLD)),
        areas[0],
    );

    let mut sessions_area = areas[1];
    if let Some(submission) = app.new_session_submission() {
        let submission_areas =
            Layout::vertical([Constraint::Length(3), Constraint::Min(1)]).split(sessions_area);
        let state = match &submission.state {
            SubmissionState::Sending => {
                Span::styled("· Starting session…", Style::default().fg(Color::Yellow))
            }
            SubmissionState::Queued => Span::styled("· Queued", Style::default().fg(Color::Yellow)),
            SubmissionState::Failed(error) => Span::styled(
                format!("× Failed — {error}"),
                Style::default().fg(Color::Red),
            ),
        };
        frame.render_widget(
            Paragraph::new(vec![
                Line::from(Span::styled(
                    ellipsize(&submission.message, usize::from(submission_areas[0].width)),
                    Style::default().add_modifier(Modifier::BOLD),
                )),
                Line::from(state),
            ]),
            submission_areas[0],
        );
        sessions_area = submission_areas[1];
    }

    if app.sessions.is_empty() {
        let empty = Paragraph::new(vec![
            Line::from(Span::styled(
                "No sessions yet",
                Style::default().add_modifier(Modifier::BOLD),
            )),
            Line::from(Span::styled(
                "Press n to start one.",
                Style::default().fg(Color::DarkGray),
            )),
        ]);
        frame.render_widget(empty, sessions_area);
        return;
    }

    let mut rows = Vec::new();
    for (index, session) in app.sessions.iter().enumerate() {
        let selected = app.selected_session == Some(index);
        let title_style = if selected {
            Style::default()
                .fg(Color::Cyan)
                .add_modifier(Modifier::BOLD)
        } else {
            Style::default().add_modifier(Modifier::BOLD)
        };
        let title = app.session_title(session.session_id());
        let title = ellipsize(&title, usize::from(sessions_area.width.saturating_sub(4)));
        let mut metadata = vec![
            Span::styled(
                session.agent_id().to_owned(),
                Style::default().fg(Color::DarkGray),
            ),
            Span::raw("  "),
        ];
        metadata.extend(status_spans(session.status()));
        let activity = match app.session_activity(session.session_id()) {
            Some(activity) => ellipsize(
                &activity,
                usize::from(sessions_area.width.saturating_sub(4)),
            ),
            None => "No activity yet".to_owned(),
        };
        rows.push(ListItem::new(vec![
            Line::from(Span::styled(title, title_style)),
            Line::from(metadata),
            Line::from(Span::styled(activity, Style::default().fg(Color::DarkGray))),
        ]));
    }
    let sessions = List::new(rows)
        .highlight_symbol("› ")
        .highlight_style(Style::default().add_modifier(Modifier::BOLD));
    let mut state = ListState::default();
    state.select(app.selected_session);
    frame.render_stateful_widget(sessions, sessions_area, &mut state);
}

fn render_timeline(frame: &mut Frame<'_>, area: Rect, app: &mut App) {
    let areas = Layout::vertical([Constraint::Length(1), Constraint::Min(1)]).split(area);
    let area = areas[1];
    let items = app.timeline_items();
    if items.is_empty() {
        app.timeline_offset(0, usize::from(area.height));
        render_timeline_position(frame, areas[0], app);
        frame.render_widget(
            Paragraph::new("Waiting for activity…").style(Style::default().fg(Color::DarkGray)),
            area,
        );
        return;
    }

    let mut lines = Vec::new();
    let width = usize::from(area.width.saturating_sub(2)).max(1);
    for item in items {
        for line in timeline_item_lines(item, width) {
            lines.push(line);
        }
    }
    let offset = app.timeline_offset(lines.len(), usize::from(area.height));
    render_timeline_position(frame, areas[0], app);
    let mut visible = Vec::new();
    for (index, line) in lines.into_iter().enumerate() {
        if index < offset {
            continue;
        }
        if visible.len() >= usize::from(area.height) {
            break;
        }
        visible.push(line);
    }
    frame.render_widget(Paragraph::new(visible), area);
}

fn render_timeline_position(frame: &mut Frame<'_>, area: Rect, app: &App) {
    let state = app.view_state();
    let line = match state.timeline_position {
        TimelinePosition::Follow => {
            Line::from(Span::styled("↓ live", Style::default().fg(Color::DarkGray)))
        }
        TimelinePosition::Offset(offset) => {
            let position = if offset == 0 {
                "↑ top".to_owned()
            } else if offset == 1 {
                "↑ 1 line above".to_owned()
            } else {
                format!("↑ {offset} lines above")
            };
            let mut spans = vec![Span::styled(position, Style::default().fg(Color::DarkGray))];
            if state.timeline_unseen_rows == 0 {
                spans.push(Span::styled(" · ", Style::default().fg(Color::DarkGray)));
            } else {
                spans.push(Span::styled(
                    format!(" · {} new · ", state.timeline_unseen_rows),
                    Style::default().fg(Color::Yellow),
                ));
            }
            spans.push(Span::styled("G", Style::default().fg(Color::Cyan)));
            spans.push(Span::styled(
                " return to live",
                Style::default().fg(Color::DarkGray),
            ));
            Line::from(spans)
        }
    };
    frame.render_widget(Paragraph::new(line).alignment(Alignment::Right), area);
}

fn render_header(frame: &mut Frame<'_>, area: Rect, app: &App) {
    let areas =
        Layout::horizontal([Constraint::Percentage(70), Constraint::Percentage(30)]).split(area);
    let mut left = vec![Line::from(Span::styled(
        "zeta",
        Style::default()
            .fg(Color::Cyan)
            .add_modifier(Modifier::BOLD),
    ))];
    if let View::Attached(_) = &app.view {
        left[0].spans.push(Span::raw("  "));
        left[0].spans.push(Span::styled(
            ellipsize(
                &app.attached_title(),
                usize::from(areas[0].width.saturating_sub(8)),
            ),
            Style::default().add_modifier(Modifier::BOLD),
        ));
        if let Some(session) = app.attached_session() {
            let mut metadata = vec![
                Span::styled(
                    session.agent_id().to_owned(),
                    Style::default().fg(Color::DarkGray),
                ),
                Span::raw("  "),
            ];
            metadata.extend(status_spans(session.status()));
            left.push(Line::from(metadata));
        }
    }
    frame.render_widget(Paragraph::new(left), areas[0]);

    let right = match app.view_state().timeline_mode {
        TimelineMode::Semantic => vec![connected_line()],
        TimelineMode::Raw => {
            let cursor = match app.cursor {
                Some(cursor) => cursor.0.to_string(),
                None => "none".to_owned(),
            };
            vec![
                Line::from(vec![
                    Span::styled("raw events", Style::default().fg(Color::Yellow)),
                    Span::styled("  ", Style::default()),
                    Span::styled("● ", Style::default().fg(Color::Green)),
                    Span::styled("connected", Style::default().fg(Color::DarkGray)),
                ]),
                Line::from(Span::styled(
                    format!("protocol {} · cursor {cursor}", app.protocol),
                    Style::default().fg(Color::DarkGray),
                )),
            ]
        }
    };
    frame.render_widget(Paragraph::new(right).alignment(Alignment::Right), areas[1]);
}

fn connected_line() -> Line<'static> {
    Line::from(vec![
        Span::styled("● ", Style::default().fg(Color::Green)),
        Span::styled("connected", Style::default().fg(Color::DarkGray)),
    ])
}

fn render_composer(frame: &mut Frame<'_>, area: Rect, app: &App) {
    let label = match &app.view {
        View::Sessions => "New session",
        View::Attached(_) => "Message",
    };
    let composing = app.mode == Mode::Compose;
    let input_width = usize::from(area.width.saturating_sub(2));
    let layout = app.view_state().draft.layout(input_width);
    let visible_height = usize::from(area.height.saturating_sub(1)).max(1);
    let title = if composing && layout.lines.len() > visible_height {
        format!(
            " {label} · line {}/{} ",
            layout.cursor_row + 1,
            layout.lines.len()
        )
    } else {
        format!(" {label} ")
    };
    let border_style = if composing {
        Style::default().fg(Color::Cyan)
    } else {
        Style::default().fg(Color::DarkGray)
    };
    let block = Block::default()
        .title(title)
        .title_style(border_style)
        .borders(Borders::TOP)
        .border_style(border_style);
    let inner = block.inner(area);
    if !composing {
        let line = Line::from(Span::styled(
            "› Message Zeta…",
            Style::default().fg(Color::DarkGray),
        ));
        frame.render_widget(Paragraph::new(line).block(block), area);
        return;
    }

    if app.view_state().draft.is_empty() {
        let prompt = match &app.view {
            View::Sessions => "What should Zeta do?",
            View::Attached(_) => "Message Zeta…",
        };
        let line = Line::from(vec![
            Span::styled("› ", Style::default().fg(Color::Cyan)),
            Span::styled(prompt, Style::default().fg(Color::DarkGray)),
        ]);
        frame.render_widget(Paragraph::new(line).block(block), area);
        frame.set_cursor_position((inner.x.saturating_add(2), inner.y));
        return;
    }

    let input_width = usize::from(inner.width.saturating_sub(2));
    let layout = app.view_state().draft.layout(input_width);
    let visible_height = usize::from(inner.height).max(1);
    let latest_start = layout.lines.len().saturating_sub(visible_height);
    let start = layout
        .cursor_row
        .saturating_sub(visible_height.saturating_sub(1))
        .min(latest_start);
    let mut lines = Vec::new();
    for line in layout.lines.iter().skip(start).take(visible_height) {
        lines.push(Line::from(vec![
            Span::styled("› ", Style::default().fg(Color::Cyan)),
            Span::raw(line.clone()),
        ]));
    }
    frame.render_widget(Paragraph::new(lines).block(block), area);

    let cursor_x = inner
        .x
        .saturating_add(2)
        .saturating_add(u16::try_from(layout.cursor_column).unwrap_or(u16::MAX))
        .min(inner.right().saturating_sub(1));
    let cursor_y = inner
        .y
        .saturating_add(u16::try_from(layout.cursor_row.saturating_sub(start)).unwrap_or(u16::MAX))
        .min(inner.bottom().saturating_sub(1));
    frame.set_cursor_position((cursor_x, cursor_y));
}

fn footer_line(app: &App) -> Line<'static> {
    let mut spans = Vec::new();
    match (&app.view, &app.mode) {
        (View::Sessions, Mode::Browse) => {
            push_key_hint(&mut spans, "↑/↓", "sessions");
            push_key_hint(&mut spans, "enter", "attach");
            push_key_hint(&mut spans, "n", "new");
            push_key_hint(&mut spans, "q", "quit");
        }
        (View::Sessions, Mode::Compose) => {
            push_key_hint(&mut spans, "enter", "start");
            if app.keyboard_enhancement {
                push_key_hint(&mut spans, "shift-enter", "newline");
            } else {
                push_key_hint(&mut spans, "ctrl-j", "newline");
            }
            push_key_hint(&mut spans, "esc", "cancel");
        }
        (View::Attached(_), Mode::Browse) => {
            push_key_hint(&mut spans, "↑/↓ pgup/pgdn", "scroll");
            push_key_hint(&mut spans, "g/G", "top/live");
            push_key_hint(&mut spans, "i", "message");
            push_key_hint(&mut spans, "esc", "detach");
            push_key_hint(&mut spans, "v", "raw");
            push_key_hint(&mut spans, "q", "quit");
        }
        (View::Attached(_), Mode::Compose) => {
            push_key_hint(&mut spans, "enter", "send");
            if app.keyboard_enhancement {
                push_key_hint(&mut spans, "shift-enter", "newline");
            } else {
                push_key_hint(&mut spans, "ctrl-j", "newline");
            }
            push_key_hint(&mut spans, "esc", "cancel");
        }
    }
    Line::from(spans)
}

fn push_key_hint(spans: &mut Vec<Span<'static>>, key: &'static str, label: &'static str) {
    if !spans.is_empty() {
        spans.push(Span::raw("   "));
    }
    spans.push(Span::styled(
        key,
        Style::default()
            .fg(Color::Cyan)
            .add_modifier(Modifier::BOLD),
    ));
    spans.push(Span::raw(" "));
    spans.push(Span::styled(label, Style::default().fg(Color::DarkGray)));
}

fn timeline_item_lines(item: TimelineItem, width: usize) -> Vec<Line<'static>> {
    match item {
        TimelineItem::User(content) => speaker_lines("You", content, width),
        TimelineItem::AgentHeading => speaker_heading("Zeta"),
        TimelineItem::Agent(content) => agent_lines(content, width),
        TimelineItem::Activity { glyph, text, color } => activity_lines(glyph, text, color, width),
        TimelineItem::Raw {
            event_type,
            payload,
        } => raw_lines(event_type, payload, width),
    }
}

fn speaker_lines(speaker: &'static str, content: String, width: usize) -> Vec<Line<'static>> {
    let mut lines = vec![Line::from(Span::styled(
        speaker,
        Style::default().add_modifier(Modifier::BOLD),
    ))];
    for line in wrap_text(&content, width.saturating_sub(2).max(1)) {
        lines.push(Line::from(format!("  {line}")));
    }
    lines.push(Line::default());
    lines
}

fn speaker_heading(speaker: &'static str) -> Vec<Line<'static>> {
    vec![Line::from(Span::styled(
        speaker,
        Style::default().add_modifier(Modifier::BOLD),
    ))]
}

fn agent_lines(content: String, width: usize) -> Vec<Line<'static>> {
    let mut lines = Vec::new();
    for line in wrap_text(&content, width.saturating_sub(2).max(1)) {
        lines.push(Line::from(format!("  {line}")));
    }
    lines.push(Line::default());
    lines
}

fn activity_lines(glyph: String, text: String, color: Color, width: usize) -> Vec<Line<'static>> {
    let wrapped = wrap_text(&text, width.saturating_sub(4).max(1));
    let mut lines = Vec::new();
    for (index, line) in wrapped.into_iter().enumerate() {
        if index == 0 {
            lines.push(Line::from(vec![
                Span::raw("  "),
                Span::styled(format!("{glyph} "), Style::default().fg(color)),
                Span::styled(line, Style::default().fg(Color::DarkGray)),
            ]));
        } else {
            lines.push(Line::from(Span::styled(
                format!("    {line}"),
                Style::default().fg(Color::DarkGray),
            )));
        }
    }
    lines
}

fn raw_lines(event_type: String, payload: String, width: usize) -> Vec<Line<'static>> {
    let mut lines = vec![Line::from(Span::styled(
        event_type,
        Style::default().fg(Color::Yellow),
    ))];
    for line in wrap_text(&payload, width.saturating_sub(2).max(1)) {
        lines.push(Line::from(Span::styled(
            format!("  {line}"),
            Style::default().fg(Color::DarkGray),
        )));
    }
    lines.push(Line::default());
    lines
}

fn wrap_text(text: &str, width: usize) -> Vec<String> {
    let mut lines = Vec::new();
    for paragraph in text.lines() {
        if paragraph.is_empty() {
            lines.push(String::new());
            continue;
        }
        let mut line = String::new();
        for word in paragraph.split_whitespace() {
            let separator = if line.is_empty() { 0 } else { 1 };
            if line.chars().count() + separator + word.chars().count() > width && !line.is_empty() {
                lines.push(line);
                line = String::new();
            }
            if !line.is_empty() {
                line.push(' ');
            }
            line.push_str(word);
        }
        lines.push(line);
    }
    if lines.is_empty() {
        lines.push(String::new());
    }
    lines
}

fn ellipsize(text: &str, width: usize) -> String {
    if text.chars().count() <= width {
        return text.to_owned();
    }
    if width <= 1 {
        return "…".to_owned();
    }
    let mut output = String::new();
    for character in text.chars() {
        if output.chars().count() == width - 1 {
            break;
        }
        output.push(character);
    }
    output.push('…');
    output
}

fn status_style(status: &str) -> Style {
    if status == "running" || status == "completed" {
        return Style::default().fg(Color::Green);
    }
    if status == "queued" || status == "waiting" {
        return Style::default().fg(Color::Yellow);
    }
    if status == "failed" || status == "cancelled" {
        return Style::default().fg(Color::Red);
    }
    Style::default().fg(Color::DarkGray)
}

fn status_spans(status: &str) -> Vec<Span<'static>> {
    let (glyph, label) = match status {
        "running" => ("● ", "Running".to_owned()),
        "queued" => ("◌ ", "Queued".to_owned()),
        "waiting" => ("◌ ", "Waiting".to_owned()),
        "completed" => ("✓ ", "Completed".to_owned()),
        "failed" => ("× ", "Failed".to_owned()),
        "cancelled" => ("× ", "Cancelled".to_owned()),
        "idle" => ("○ ", "Idle".to_owned()),
        other => ("○ ", title_case(&humanize(other))),
    };
    vec![
        Span::styled(glyph, status_style(status)),
        Span::styled(label, status_style(status)),
    ]
}

fn title_case(text: &str) -> String {
    let mut characters = text.chars();
    let Some(first) = characters.next() else {
        return String::new();
    };
    let mut output = first.to_uppercase().collect::<String>();
    output.extend(characters);
    output
}

fn padded_area(area: Rect) -> Rect {
    let horizontal = if area.width >= 40 { 2 } else { 1 };
    let vertical = if area.height >= 10 { 1 } else { 0 };
    area.inner(Margin {
        horizontal,
        vertical,
    })
}

#[cfg(test)]
mod tests {
    use crossterm::event::{Event as TerminalEvent, KeyCode, KeyEvent, KeyModifiers};
    use ratatui::Terminal;
    use ratatui::backend::TestBackend;
    use ratatui::layout::Rect;
    use ratatui::style::Modifier;

    use super::{App, AppAction, draw};
    use crate::wire::{Cursor, Event, Session};

    fn event(
        event_type: &str,
        payload: serde_json::Value,
        session_id: &str,
        run_id: &str,
        cursor: u64,
    ) -> Event {
        event_with_idempotency(event_type, payload, session_id, run_id, cursor, None)
    }

    fn event_with_idempotency(
        event_type: &str,
        payload: serde_json::Value,
        session_id: &str,
        run_id: &str,
        cursor: u64,
        idempotency_key: Option<&str>,
    ) -> Event {
        serde_json::from_value(serde_json::json!({
            "id": format!("evt_{cursor}"),
            "event_type": event_type,
            "source": "user",
            "payload": payload,
            "idempotency_key": idempotency_key,
            "caused_by": null,
            "session_id": session_id,
            "run_id": run_id,
            "turn_id": null,
            "timestamp_ms": 1_754_438_400_000_i64 + cursor as i64,
            "cursor": cursor
        }))
        .expect("event should parse")
    }

    fn session(session_id: &str, agent_id: &str, status: &str) -> Session {
        serde_json::from_value(serde_json::json!({
            "session_id": session_id,
            "agent_id": agent_id,
            "status": status
        }))
        .expect("session should parse")
    }

    fn text_position(screen: &str, text: &str) -> (u16, u16) {
        for (row, line) in screen.lines().enumerate() {
            if let Some(column) = line.find(text) {
                return (column as u16, row as u16);
            }
        }
        panic!("{text:?} should be visible");
    }

    #[test]
    fn startup_renders_only_the_session_list() {
        let request = event(
            "session.message.requested",
            serde_json::json!({"message": "Summarize the current project"}),
            "session_1",
            "run_1",
            1,
        );
        let mut app = App::connected(
            "0.1".to_owned(),
            vec![session("session_1", "zeta.master", "queued")],
            vec![request],
            Some(Cursor(1)),
        );
        let backend = TestBackend::new(80, 18);
        let mut terminal = Terminal::new(backend).expect("terminal should initialize");

        terminal
            .draw(|frame| draw(frame, &mut app))
            .expect("screen should draw");
        let screen = terminal.backend().to_string();

        assert!(screen.contains("Sessions"));
        assert!(screen.contains("Summarize the current project"));
        assert!(screen.contains("zeta.master"));
        assert!(screen.contains("◌ Queued"));
        assert!(!screen.contains("Timeline"));
        assert!(!screen.contains("Message"));
        assert!(!screen.contains("protocol 0.1"));
        assert!(!screen.contains("cursor 1"));
        assert!(screen.contains("enter attach"));
        assert!(screen.contains("n new"));
    }

    #[test]
    fn empty_session_list_teaches_the_primary_action() {
        let mut app = App::connected("0.1".to_owned(), Vec::new(), Vec::new(), None);
        let backend = TestBackend::new(80, 18);
        let mut terminal = Terminal::new(backend).expect("terminal should initialize");

        terminal
            .draw(|frame| draw(frame, &mut app))
            .expect("screen should draw");
        let screen = terminal.backend().to_string();

        assert!(screen.contains("No sessions yet"));
        assert!(screen.contains("Press n to start one."));
    }

    #[test]
    fn narrow_terminal_keeps_the_primary_controls_visible() {
        let request = event(
            "session.message.requested",
            serde_json::json!({"message": "A title that cannot fit on a narrow terminal"}),
            "session_1",
            "run_1",
            1,
        );
        let mut app = App::connected(
            "0.1".to_owned(),
            vec![session("session_1", "zeta.master", "running")],
            vec![request],
            Some(Cursor(1)),
        );
        let backend = TestBackend::new(40, 10);
        let mut terminal = Terminal::new(backend).expect("terminal should initialize");

        terminal
            .draw(|frame| draw(frame, &mut app))
            .expect("narrow screen should draw");
        let screen = terminal.backend().to_string();

        assert!(screen.contains("Sessions"));
        assert!(screen.contains("A title that cannot"));
        assert!(screen.contains("n new"));
    }

    #[test]
    fn attached_timeline_renders_conversation_and_activity_instead_of_wire_events() {
        let request = event(
            "session.message.requested",
            serde_json::json!({"message": "Inspect the project"}),
            "session_1",
            "run_1",
            1,
        );
        let queued = event(
            "runtime.queue_item.available",
            serde_json::json!({"queue_item_id": "queue_1"}),
            "session_1",
            "run_1",
            2,
        );
        let thinking = event(
            "runtime.status.update",
            serde_json::json!({"status": "reasoning_delta"}),
            "session_1",
            "run_1",
            3,
        );
        let tool_started = event(
            "zeta.tool_call.started",
            serde_json::json!({
                "_timeline_type": "tool_call",
                "tool_call_id": "call_1",
                "name": "read",
                "input": {"path": "README.md"}
            }),
            "session_1",
            "run_1",
            4,
        );
        let tool_completed = event(
            "zeta.tool_call.completed",
            serde_json::json!({
                "_timeline_type": "tool_result",
                "tool_call_id": "call_1",
                "name": "read",
                "result": {"ok": true}
            }),
            "session_1",
            "run_1",
            5,
        );
        let model = event(
            "zeta.model_call.completed",
            serde_json::json!({
                "_timeline_type": "model",
                "content": "The project is ready for the next step."
            }),
            "session_1",
            "run_1",
            6,
        );
        let completed = event(
            "zeta.turn.completed",
            serde_json::json!({"content": "done"}),
            "session_1",
            "run_1",
            7,
        );
        let mut app = App::connected(
            "0.1".to_owned(),
            vec![session("session_1", "zeta.master", "idle")],
            vec![
                request,
                queued,
                thinking,
                tool_started,
                tool_completed,
                model,
                completed,
            ],
            Some(Cursor(7)),
        );
        let enter = TerminalEvent::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert_eq!(app.handle_event(&enter), AppAction::None);
        let backend = TestBackend::new(100, 24);
        let mut terminal = Terminal::new(backend).expect("terminal should initialize");

        terminal
            .draw(|frame| draw(frame, &mut app))
            .expect("screen should draw");
        let screen = terminal.backend().to_string();

        assert!(screen.contains("You"));
        assert!(screen.contains("Inspect the project"));
        assert!(screen.contains("Zeta"));
        assert!(screen.contains("The project is ready for the next step."));
        assert!(screen.contains("read README.md"));
        assert!(screen.contains("Completed"));
        assert!(!screen.contains("runtime.queue_item.available"));
        assert!(!screen.contains("queue_item_id"));
        assert!(screen.contains("v raw"));

        let (_, metadata_row) = text_position(&screen, "zeta.master");
        let (you_column, you_row) = text_position(&screen, "You");
        let (zeta_column, zeta_row) = text_position(&screen, "Zeta");
        let (_, thinking_row) = text_position(&screen, "Thinking");
        let (_, tool_row) = text_position(&screen, "read README.md");

        assert!(you_row > metadata_row + 1);
        assert!(zeta_row < thinking_row);
        assert!(zeta_row < tool_row);
        assert!(
            terminal.backend().buffer()[(you_column, you_row)]
                .modifier
                .contains(Modifier::BOLD)
        );
        assert!(
            terminal.backend().buffer()[(zeta_column, zeta_row)]
                .modifier
                .contains(Modifier::BOLD)
        );
    }

    #[test]
    fn attached_timeline_scrolls_through_a_message_taller_than_the_viewport() {
        let request = event(
            "session.message.requested",
            serde_json::json!({"message": "Inspect the project"}),
            "session_1",
            "run_1",
            1,
        );
        let mut content = String::new();
        for row in 0..12 {
            content.push_str(&format!("row-{row:02}-xxxxxxxxxxxxxxxxxxxxxxxx "));
        }
        let model = event(
            "zeta.model_call.completed",
            serde_json::json!({
                "_timeline_type": "model",
                "content": content
            }),
            "session_1",
            "run_1",
            2,
        );
        let mut app = App::connected(
            "0.1".to_owned(),
            vec![session("session_1", "zeta.master", "running")],
            vec![request, model],
            Some(Cursor(2)),
        );
        let enter = TerminalEvent::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert_eq!(app.handle_event(&enter), AppAction::None);
        let backend = TestBackend::new(44, 16);
        let mut terminal = Terminal::new(backend).expect("terminal should initialize");

        terminal
            .draw(|frame| draw(frame, &mut app))
            .expect("screen should draw");
        let screen = terminal.backend().to_string();
        assert!(screen.contains("row-11-xxxxxxxxxxxxxxxxxxxxxxxx"));
        assert!(!screen.contains("row-04-xxxxxxxxxxxxxxxxxxxxxxxx"));

        let up = TerminalEvent::Key(KeyEvent::new(KeyCode::Up, KeyModifiers::NONE));
        assert_eq!(app.handle_event(&up), AppAction::None);
        terminal
            .draw(|frame| draw(frame, &mut app))
            .expect("screen should draw");
        let screen = terminal.backend().to_string();
        assert!(screen.contains("row-04-xxxxxxxxxxxxxxxxxxxxxxxx"));
        assert!(screen.contains("row-11-xxxxxxxxxxxxxxxxxxxxxxxx"));

        let completed = event(
            "zeta.turn.completed",
            serde_json::json!({"content": "done"}),
            "session_1",
            "run_1",
            3,
        );
        app.append_events(vec![completed], Some(Cursor(3)));
        terminal
            .draw(|frame| draw(frame, &mut app))
            .expect("screen should draw");
        let screen = terminal.backend().to_string();
        assert!(screen.contains("row-04-xxxxxxxxxxxxxxxxxxxxxxxx"));
        assert!(!screen.contains("Completed"));

        let down = TerminalEvent::Key(KeyEvent::new(KeyCode::Down, KeyModifiers::NONE));
        assert_eq!(app.handle_event(&down), AppAction::None);
        assert_eq!(app.handle_event(&down), AppAction::None);
        terminal
            .draw(|frame| draw(frame, &mut app))
            .expect("screen should draw");
        let screen = terminal.backend().to_string();
        assert!(screen.contains("Completed"));

        let waiting = event(
            "runtime.status.update",
            serde_json::json!({"status": "waiting", "text": "Waiting for approval"}),
            "session_1",
            "run_1",
            4,
        );
        app.append_events(vec![waiting], Some(Cursor(4)));
        terminal
            .draw(|frame| draw(frame, &mut app))
            .expect("screen should draw");
        let screen = terminal.backend().to_string();
        assert!(screen.contains("Waiting for approval"));
    }

    #[test]
    fn attached_timeline_jumps_between_top_pages_and_live_output() {
        let request = event(
            "session.message.requested",
            serde_json::json!({"message": "Inspect the project"}),
            "session_1",
            "run_1",
            1,
        );
        let mut content = String::new();
        for row in 0..18 {
            content.push_str(&format!("row-{row:02}-xxxxxxxxxxxxxxxxxxxxxxxx "));
        }
        let model = event(
            "zeta.model_call.completed",
            serde_json::json!({"content": content}),
            "session_1",
            "run_1",
            2,
        );
        let mut app = App::connected(
            "0.1".to_owned(),
            vec![session("session_1", "zeta.master", "completed")],
            vec![request, model],
            Some(Cursor(2)),
        );
        let enter = TerminalEvent::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        let top = TerminalEvent::Key(KeyEvent::new(KeyCode::Char('g'), KeyModifiers::NONE));
        let live = TerminalEvent::Key(KeyEvent::new(KeyCode::Char('G'), KeyModifiers::SHIFT));
        let down = TerminalEvent::Key(KeyEvent::new(KeyCode::Down, KeyModifiers::NONE));
        let page_up = TerminalEvent::Key(KeyEvent::new(KeyCode::PageUp, KeyModifiers::NONE));
        let page_down = TerminalEvent::Key(KeyEvent::new(KeyCode::PageDown, KeyModifiers::NONE));
        assert_eq!(app.handle_event(&enter), AppAction::None);
        let backend = TestBackend::new(44, 16);
        let mut terminal = Terminal::new(backend).expect("terminal should initialize");
        terminal
            .draw(|frame| draw(frame, &mut app))
            .expect("live timeline should draw");
        assert!(terminal.backend().to_string().contains("↓ live"));

        assert_eq!(app.handle_event(&top), AppAction::None);
        terminal
            .draw(|frame| draw(frame, &mut app))
            .expect("top timeline should draw");
        let screen = terminal.backend().to_string();
        assert!(screen.contains("row-00-xxxxxxxxxxxxxxxxxxxxxxxx"));
        assert!(screen.contains("↑ top"));
        assert!(!screen.contains("0 lines above"));
        assert!(!screen.contains("0 new"));

        assert_eq!(app.handle_event(&down), AppAction::None);
        terminal
            .draw(|frame| draw(frame, &mut app))
            .expect("next line should draw");
        assert!(terminal.backend().to_string().contains("↑ 1 line above"));

        assert_eq!(app.handle_event(&top), AppAction::None);

        assert_eq!(app.handle_event(&page_down), AppAction::None);
        terminal
            .draw(|frame| draw(frame, &mut app))
            .expect("next page should draw");
        assert!(!terminal.backend().to_string().contains("↑ top"));

        assert_eq!(app.handle_event(&page_up), AppAction::None);
        terminal
            .draw(|frame| draw(frame, &mut app))
            .expect("previous page should draw");
        assert!(terminal.backend().to_string().contains("↑ top"));

        assert_eq!(app.handle_event(&live), AppAction::None);
        terminal
            .draw(|frame| draw(frame, &mut app))
            .expect("live timeline should draw");
        let screen = terminal.backend().to_string();
        assert!(screen.contains("row-17-xxxxxxxxxxxxxxxxxxxxxxxx"));
        assert!(screen.contains("↓ live"));
    }

    #[test]
    fn anchored_history_counts_new_rows_until_returning_live() {
        let request = event(
            "session.message.requested",
            serde_json::json!({"message": "Inspect the project"}),
            "session_1",
            "run_1",
            1,
        );
        let mut content = String::new();
        for row in 0..12 {
            content.push_str(&format!("row-{row:02}-xxxxxxxxxxxxxxxxxxxxxxxx "));
        }
        let model = event(
            "zeta.model_call.completed",
            serde_json::json!({"content": content}),
            "session_1",
            "run_1",
            2,
        );
        let mut app = App::connected(
            "0.1".to_owned(),
            vec![session("session_1", "zeta.master", "running")],
            vec![request, model],
            Some(Cursor(2)),
        );
        let enter = TerminalEvent::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        let up = TerminalEvent::Key(KeyEvent::new(KeyCode::Up, KeyModifiers::NONE));
        let live = TerminalEvent::Key(KeyEvent::new(KeyCode::Char('G'), KeyModifiers::SHIFT));
        assert_eq!(app.handle_event(&enter), AppAction::None);
        let backend = TestBackend::new(64, 16);
        let mut terminal = Terminal::new(backend).expect("terminal should initialize");
        terminal
            .draw(|frame| draw(frame, &mut app))
            .expect("live timeline should draw");
        assert_eq!(app.handle_event(&up), AppAction::None);

        let completed = event(
            "zeta.turn.completed",
            serde_json::json!({"content": "done"}),
            "session_1",
            "run_1",
            3,
        );
        app.append_events(vec![completed], Some(Cursor(3)));
        terminal
            .draw(|frame| draw(frame, &mut app))
            .expect("anchored history should draw");
        let screen = terminal.backend().to_string();
        assert!(screen.contains("1 new"));
        assert!(screen.contains("G return to live"));
        assert!(!screen.contains("Completed"));

        assert_eq!(app.handle_event(&live), AppAction::None);
        terminal
            .draw(|frame| draw(frame, &mut app))
            .expect("live timeline should draw");
        let screen = terminal.backend().to_string();
        assert!(screen.contains("↓ live"));
        assert!(screen.contains("Completed"));
        assert!(!screen.contains("1 new"));
    }

    #[test]
    fn raw_timeline_toggle_exposes_protocol_details() {
        let request = event(
            "session.message.requested",
            serde_json::json!({"message": "Inspect the project"}),
            "session_1",
            "run_1",
            1,
        );
        let queued = event(
            "runtime.queue_item.available",
            serde_json::json!({"queue_item_id": "queue_1"}),
            "session_1",
            "run_1",
            2,
        );
        let mut app = App::connected(
            "0.1".to_owned(),
            vec![session("session_1", "zeta.master", "queued")],
            vec![request, queued],
            Some(Cursor(2)),
        );
        let enter = TerminalEvent::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        let verbose = TerminalEvent::Key(KeyEvent::new(KeyCode::Char('v'), KeyModifiers::NONE));
        assert_eq!(app.handle_event(&enter), AppAction::None);
        assert_eq!(app.handle_event(&verbose), AppAction::None);
        let backend = TestBackend::new(100, 20);
        let mut terminal = Terminal::new(backend).expect("terminal should initialize");

        terminal
            .draw(|frame| draw(frame, &mut app))
            .expect("screen should draw");
        let screen = terminal.backend().to_string();

        assert!(screen.contains("raw events"));
        assert!(screen.contains("protocol 0.1"));
        assert!(screen.contains("cursor 2"));
        assert!(screen.contains("runtime.queue_item.available"));
        assert!(screen.contains("queue_item_id"));
    }

    #[test]
    fn enter_attaches_and_escape_returns_to_sessions() {
        let selected_event = event(
            "zeta.user_message",
            serde_json::json!({"content": "selected progress"}),
            "session_1",
            "run_1",
            1,
        );
        let other_event = event(
            "zeta.user_message",
            serde_json::json!({"content": "other progress"}),
            "session_2",
            "run_2",
            2,
        );
        let mut app = App::connected(
            "0.1".to_owned(),
            vec![
                session("session_1", "zeta.master", "running"),
                session("session_2", "reviewer", "idle"),
            ],
            vec![selected_event, other_event],
            Some(Cursor(2)),
        );
        let enter = TerminalEvent::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        let escape = TerminalEvent::Key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        let backend = TestBackend::new(80, 18);
        let mut terminal = Terminal::new(backend).expect("terminal should initialize");

        assert_eq!(app.handle_event(&enter), AppAction::None);
        terminal
            .draw(|frame| draw(frame, &mut app))
            .expect("attached screen should draw");
        let screen = terminal.backend().to_string();
        assert!(screen.contains("zeta.master"));
        assert!(screen.contains("● Running"));
        assert!(screen.contains("selected progress"));
        assert!(!screen.contains("other progress"));
        assert!(!screen.contains("Sessions"));

        assert_eq!(app.handle_event(&escape), AppAction::None);
        terminal
            .draw(|frame| draw(frame, &mut app))
            .expect("detached screen should draw");
        let screen = terminal.backend().to_string();
        assert!(screen.contains("Sessions"));
        assert!(screen.contains("selected progress"));
        assert!(!screen.contains("Message Zeta"));
    }

    #[test]
    fn new_and_attached_composers_route_by_attachment() {
        let mut app = App::connected(
            "0.1".to_owned(),
            vec![session("session_1", "zeta.master", "idle")],
            Vec::new(),
            None,
        );
        let new = TerminalEvent::Key(KeyEvent::new(KeyCode::Char('n'), KeyModifiers::NONE));
        let compose = TerminalEvent::Key(KeyEvent::new(KeyCode::Char('i'), KeyModifiers::NONE));
        let h = TerminalEvent::Key(KeyEvent::new(KeyCode::Char('h'), KeyModifiers::NONE));
        let i = TerminalEvent::Key(KeyEvent::new(KeyCode::Char('i'), KeyModifiers::NONE));
        let enter = TerminalEvent::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));

        assert_eq!(app.handle_event(&new), AppAction::None);
        assert_eq!(app.handle_event(&h), AppAction::None);
        assert_eq!(app.handle_event(&i), AppAction::None);
        assert_eq!(app.handle_event(&enter), AppAction::Submit("hi".to_owned()));
        assert_eq!(app.attached_session_id(), None);

        assert_eq!(app.handle_event(&enter), AppAction::None);
        assert_eq!(app.attached_session_id(), Some("session_1"));
        assert_eq!(app.handle_event(&compose), AppAction::None);
        assert_eq!(app.handle_event(&h), AppAction::None);
        assert_eq!(app.handle_event(&i), AppAction::None);
        assert_eq!(app.handle_event(&enter), AppAction::Submit("hi".to_owned()));
        assert_eq!(app.attached_session_id(), Some("session_1"));
    }

    #[test]
    fn composer_edits_utf8_at_the_cursor_and_preserves_multiline_paste() {
        let mut app = App::connected("0.1".to_owned(), Vec::new(), Vec::new(), None);
        let new = TerminalEvent::Key(KeyEvent::new(KeyCode::Char('n'), KeyModifiers::NONE));
        let a = TerminalEvent::Key(KeyEvent::new(KeyCode::Char('a'), KeyModifiers::NONE));
        let c = TerminalEvent::Key(KeyEvent::new(KeyCode::Char('c'), KeyModifiers::NONE));
        let accent = TerminalEvent::Key(KeyEvent::new(KeyCode::Char('é'), KeyModifiers::NONE));
        let emoji = TerminalEvent::Key(KeyEvent::new(KeyCode::Char('🙂'), KeyModifiers::NONE));
        let left = TerminalEvent::Key(KeyEvent::new(KeyCode::Left, KeyModifiers::NONE));
        let home = TerminalEvent::Key(KeyEvent::new(KeyCode::Home, KeyModifiers::NONE));
        let end = TerminalEvent::Key(KeyEvent::new(KeyCode::End, KeyModifiers::NONE));
        let delete = TerminalEvent::Key(KeyEvent::new(KeyCode::Delete, KeyModifiers::NONE));
        let delete_word =
            TerminalEvent::Key(KeyEvent::new(KeyCode::Char('w'), KeyModifiers::CONTROL));
        let enter = TerminalEvent::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));

        assert_eq!(app.handle_event(&new), AppAction::None);
        assert_eq!(app.handle_event(&a), AppAction::None);
        assert_eq!(app.handle_event(&c), AppAction::None);
        assert_eq!(app.handle_event(&left), AppAction::None);
        assert_eq!(app.handle_event(&accent), AppAction::None);
        assert_eq!(app.handle_event(&home), AppAction::None);
        assert_eq!(app.handle_event(&delete), AppAction::None);
        assert_eq!(app.handle_event(&end), AppAction::None);
        assert_eq!(
            app.handle_event(&TerminalEvent::Paste("\nnext word".to_owned())),
            AppAction::None
        );
        assert_eq!(app.handle_event(&delete_word), AppAction::None);
        assert_eq!(app.handle_event(&emoji), AppAction::None);

        assert_eq!(
            app.handle_event(&enter),
            AppAction::Submit("éc\nnext🙂".to_owned())
        );
    }

    #[test]
    fn composer_backspace_deletes_before_the_cursor_and_escape_preserves_the_draft() {
        let mut app = App::connected("0.1".to_owned(), Vec::new(), Vec::new(), None);
        let new = TerminalEvent::Key(KeyEvent::new(KeyCode::Char('n'), KeyModifiers::NONE));
        let a = TerminalEvent::Key(KeyEvent::new(KeyCode::Char('a'), KeyModifiers::NONE));
        let b = TerminalEvent::Key(KeyEvent::new(KeyCode::Char('b'), KeyModifiers::NONE));
        let left = TerminalEvent::Key(KeyEvent::new(KeyCode::Left, KeyModifiers::NONE));
        let backspace = TerminalEvent::Key(KeyEvent::new(KeyCode::Backspace, KeyModifiers::NONE));
        let escape = TerminalEvent::Key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        let enter = TerminalEvent::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));

        assert_eq!(app.handle_event(&new), AppAction::None);
        assert_eq!(app.handle_event(&a), AppAction::None);
        assert_eq!(app.handle_event(&b), AppAction::None);
        assert_eq!(app.handle_event(&left), AppAction::None);
        assert_eq!(app.handle_event(&backspace), AppAction::None);
        assert_eq!(app.handle_event(&enter), AppAction::Submit("b".to_owned()));

        assert_eq!(app.handle_event(&new), AppAction::None);
        assert_eq!(app.handle_event(&a), AppAction::None);
        assert_eq!(app.handle_event(&escape), AppAction::None);
        assert_eq!(app.handle_event(&new), AppAction::None);
        assert_eq!(app.handle_event(&enter), AppAction::Submit("a".to_owned()));
    }

    #[test]
    fn sessions_preserve_independent_drafts_and_history_positions() {
        let mut first_content = String::new();
        let mut second_content = String::new();
        for row in 0..18 {
            first_content.push_str(&format!("first-{row:02}-xxxxxxxxxxxxxxxxxxxxxxxx "));
            second_content.push_str(&format!("second-{row:02}-xxxxxxxxxxxxxxxxxxxxxxx "));
        }
        let first = event(
            "zeta.model_call.completed",
            serde_json::json!({"content": first_content}),
            "session_1",
            "run_1",
            1,
        );
        let second = event(
            "zeta.model_call.completed",
            serde_json::json!({"content": second_content}),
            "session_2",
            "run_2",
            2,
        );
        let mut app = App::connected(
            "0.1".to_owned(),
            vec![
                session("session_1", "zeta.master", "idle"),
                session("session_2", "zeta.master", "idle"),
            ],
            vec![first, second],
            Some(Cursor(2)),
        );
        let enter = TerminalEvent::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        let compose = TerminalEvent::Key(KeyEvent::new(KeyCode::Char('i'), KeyModifiers::NONE));
        let escape = TerminalEvent::Key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        let down = TerminalEvent::Key(KeyEvent::new(KeyCode::Down, KeyModifiers::NONE));
        let up = TerminalEvent::Key(KeyEvent::new(KeyCode::Up, KeyModifiers::NONE));
        let page_up = TerminalEvent::Key(KeyEvent::new(KeyCode::PageUp, KeyModifiers::NONE));
        let backend = TestBackend::new(48, 16);
        let mut terminal = Terminal::new(backend).expect("terminal should initialize");

        assert_eq!(app.handle_event(&enter), AppAction::None);
        terminal
            .draw(|frame| draw(frame, &mut app))
            .expect("first session should draw");
        assert_eq!(app.handle_event(&page_up), AppAction::None);
        assert_eq!(app.handle_event(&compose), AppAction::None);
        assert_eq!(
            app.handle_event(&TerminalEvent::Paste("first draft".to_owned())),
            AppAction::None
        );
        assert_eq!(app.handle_event(&escape), AppAction::None);
        assert_eq!(app.handle_event(&escape), AppAction::None);
        app.append_events(
            vec![event(
                "zeta.turn.completed",
                serde_json::json!({"content": "new first output"}),
                "session_1",
                "run_1",
                3,
            )],
            Some(Cursor(3)),
        );

        assert_eq!(app.handle_event(&down), AppAction::None);
        assert_eq!(app.handle_event(&enter), AppAction::None);
        terminal
            .draw(|frame| draw(frame, &mut app))
            .expect("second session should draw");
        assert!(terminal.backend().to_string().contains("↓ live"));
        assert_eq!(app.handle_event(&compose), AppAction::None);
        assert_eq!(
            app.handle_event(&TerminalEvent::Paste("second draft".to_owned())),
            AppAction::None
        );
        assert_eq!(app.handle_event(&escape), AppAction::None);
        assert_eq!(app.handle_event(&escape), AppAction::None);

        assert_eq!(app.handle_event(&up), AppAction::None);
        assert_eq!(app.handle_event(&enter), AppAction::None);
        terminal
            .draw(|frame| draw(frame, &mut app))
            .expect("first session should restore");
        let screen = terminal.backend().to_string();
        assert!(screen.contains("↑"));
        assert!(app.view_state().timeline_unseen_rows > 0);
        assert_eq!(app.handle_event(&compose), AppAction::None);
        terminal
            .draw(|frame| draw(frame, &mut app))
            .expect("first draft should restore");
        let screen = terminal.backend().to_string();
        assert!(screen.contains("first draft"));
        assert!(!screen.contains("second draft"));
        assert_eq!(
            app.handle_event(&enter),
            AppAction::Submit("first draft".to_owned())
        );
    }

    #[test]
    fn composer_renders_contextual_prompt_and_cursor_for_wrapped_input() {
        let mut app = App::connected(
            "0.1".to_owned(),
            vec![session("session_1", "zeta.master", "idle")],
            Vec::new(),
            None,
        );
        let new = TerminalEvent::Key(KeyEvent::new(KeyCode::Char('n'), KeyModifiers::NONE));
        let enter = TerminalEvent::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        let compose = TerminalEvent::Key(KeyEvent::new(KeyCode::Char('i'), KeyModifiers::NONE));
        let escape = TerminalEvent::Key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        let backend = TestBackend::new(36, 14);
        let mut terminal = Terminal::new(backend).expect("terminal should initialize");

        assert_eq!(app.handle_event(&new), AppAction::None);
        terminal
            .draw(|frame| draw(frame, &mut app))
            .expect("new-session composer should draw");
        assert!(
            terminal
                .backend()
                .to_string()
                .contains("What should Zeta do?")
        );

        assert_eq!(app.handle_event(&escape), AppAction::None);
        assert_eq!(app.handle_event(&enter), AppAction::None);
        assert_eq!(app.handle_event(&compose), AppAction::None);
        assert_eq!(
            app.handle_event(&TerminalEvent::Paste(
                "first line wraps across the composer\nsecond".to_owned()
            )),
            AppAction::None
        );
        terminal
            .draw(|frame| draw(frame, &mut app))
            .expect("attached composer should draw");
        let screen = terminal.backend().to_string();
        assert!(screen.contains("first line wraps"));
        assert!(screen.contains("second"));
        assert!(terminal.get_cursor_position().is_ok());
    }

    #[test]
    fn composer_supports_kill_yank_and_modified_enter_newlines() {
        let mut app = App::connected("0.1".to_owned(), Vec::new(), Vec::new(), None);
        let new = TerminalEvent::Key(KeyEvent::new(KeyCode::Char('n'), KeyModifiers::NONE));
        let control_a =
            TerminalEvent::Key(KeyEvent::new(KeyCode::Char('a'), KeyModifiers::CONTROL));
        let control_e =
            TerminalEvent::Key(KeyEvent::new(KeyCode::Char('e'), KeyModifiers::CONTROL));
        let control_k =
            TerminalEvent::Key(KeyEvent::new(KeyCode::Char('k'), KeyModifiers::CONTROL));
        let control_u =
            TerminalEvent::Key(KeyEvent::new(KeyCode::Char('u'), KeyModifiers::CONTROL));
        let control_y =
            TerminalEvent::Key(KeyEvent::new(KeyCode::Char('y'), KeyModifiers::CONTROL));
        let control_j =
            TerminalEvent::Key(KeyEvent::new(KeyCode::Char('j'), KeyModifiers::CONTROL));
        let newline = TerminalEvent::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::SHIFT));
        let enter = TerminalEvent::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));

        assert_eq!(app.handle_event(&new), AppAction::None);
        assert_eq!(
            app.handle_event(&TerminalEvent::Paste("alpha beta".to_owned())),
            AppAction::None
        );
        assert_eq!(app.handle_event(&control_a), AppAction::None);
        assert_eq!(app.handle_event(&control_k), AppAction::None);
        assert_eq!(app.handle_event(&control_y), AppAction::None);
        assert_eq!(app.handle_event(&control_e), AppAction::None);
        assert_eq!(app.handle_event(&control_u), AppAction::None);
        assert_eq!(app.handle_event(&control_y), AppAction::None);
        assert_eq!(app.handle_event(&newline), AppAction::None);
        assert_eq!(
            app.handle_event(&TerminalEvent::Paste("tail".to_owned())),
            AppAction::None
        );
        assert_eq!(app.handle_event(&control_j), AppAction::None);
        assert_eq!(
            app.handle_event(&TerminalEvent::Paste("last".to_owned())),
            AppAction::None
        );
        assert_eq!(
            app.handle_event(&enter),
            AppAction::Submit("alpha beta\ntail\nlast".to_owned())
        );
    }

    #[test]
    fn submitted_message_history_restores_the_unsubmitted_draft_per_session() {
        let mut app = App::connected(
            "0.1".to_owned(),
            vec![
                session("session_1", "zeta.master", "idle"),
                session("session_2", "zeta.master", "idle"),
            ],
            Vec::new(),
            None,
        );
        let enter = TerminalEvent::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        let compose = TerminalEvent::Key(KeyEvent::new(KeyCode::Char('i'), KeyModifiers::NONE));
        let previous = TerminalEvent::Key(KeyEvent::new(KeyCode::Char('p'), KeyModifiers::CONTROL));
        let next = TerminalEvent::Key(KeyEvent::new(KeyCode::Char('n'), KeyModifiers::CONTROL));
        let escape = TerminalEvent::Key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        let down = TerminalEvent::Key(KeyEvent::new(KeyCode::Down, KeyModifiers::NONE));

        assert_eq!(app.handle_event(&enter), AppAction::None);
        assert_eq!(app.handle_event(&compose), AppAction::None);
        assert_eq!(
            app.handle_event(&TerminalEvent::Paste("first command".to_owned())),
            AppAction::None
        );
        assert_eq!(
            app.handle_event(&enter),
            AppAction::Submit("first command".to_owned())
        );
        assert_eq!(app.handle_event(&compose), AppAction::None);
        assert_eq!(
            app.handle_event(&TerminalEvent::Paste("working draft".to_owned())),
            AppAction::None
        );
        assert_eq!(app.handle_event(&previous), AppAction::None);
        assert_eq!(app.handle_event(&next), AppAction::None);
        assert_eq!(
            app.handle_event(&enter),
            AppAction::Submit("working draft".to_owned())
        );

        assert_eq!(app.handle_event(&escape), AppAction::None);
        assert_eq!(app.handle_event(&down), AppAction::None);
        assert_eq!(app.handle_event(&enter), AppAction::None);
        assert_eq!(app.handle_event(&compose), AppAction::None);
        assert_eq!(app.handle_event(&previous), AppAction::None);
        assert_eq!(app.handle_event(&enter), AppAction::None);
    }

    #[test]
    fn long_composer_shows_position_and_keyboard_specific_newline_hint() {
        let mut app = App::connected("0.1".to_owned(), Vec::new(), Vec::new(), None);
        let new = TerminalEvent::Key(KeyEvent::new(KeyCode::Char('n'), KeyModifiers::NONE));
        assert_eq!(app.handle_event(&new), AppAction::None);
        assert_eq!(
            app.handle_event(&TerminalEvent::Paste(
                "one\ntwo\nthree\nfour\nfive\nsix\nseven\neight\nnine".to_owned()
            )),
            AppAction::None
        );
        app.set_keyboard_enhancement(true);
        let backend = TestBackend::new(60, 18);
        let mut terminal = Terminal::new(backend).expect("terminal should initialize");
        terminal
            .draw(|frame| draw(frame, &mut app))
            .expect("long composer should draw");
        let screen = terminal.backend().to_string();

        assert!(screen.contains("line 9/9"));
        assert!(screen.contains("shift-enter newline"));
        assert!(!screen.contains("ctrl-j newline"));
    }

    #[test]
    fn attached_submission_echoes_sending_and_queued_before_durable_reconciliation() {
        let mut app = App::connected(
            "0.1".to_owned(),
            vec![session("session_1", "zeta.master", "idle")],
            Vec::new(),
            None,
        );
        let enter = TerminalEvent::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert_eq!(app.handle_event(&enter), AppAction::None);

        app.submission_started("client-1".to_owned(), "hello".to_owned());
        assert_eq!(
            app.timeline_items(),
            vec![
                super::TimelineItem::User("hello".to_owned()),
                super::TimelineItem::Activity {
                    glyph: "·".to_owned(),
                    text: "Sending…".to_owned(),
                    color: ratatui::style::Color::Yellow,
                },
            ]
        );

        app.submission_queued("client-1", "evt_2", "session_1");
        assert_eq!(
            app.timeline_items(),
            vec![
                super::TimelineItem::User("hello".to_owned()),
                super::TimelineItem::Activity {
                    glyph: "·".to_owned(),
                    text: "Queued".to_owned(),
                    color: ratatui::style::Color::Yellow,
                },
            ]
        );

        let request = event(
            "session.message.requested",
            serde_json::json!({"message": "hello"}),
            "session_1",
            "run_1",
            2,
        );
        app.append_events(vec![request], Some(Cursor(2)));
        assert_eq!(
            app.timeline_items(),
            vec![super::TimelineItem::User("hello".to_owned())]
        );
    }

    #[test]
    fn durable_submission_reconciles_even_when_its_event_arrives_before_the_rpc_response() {
        let mut app = App::connected(
            "0.1".to_owned(),
            vec![session("session_1", "zeta.master", "idle")],
            Vec::new(),
            None,
        );
        let enter = TerminalEvent::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert_eq!(app.handle_event(&enter), AppAction::None);
        app.submission_started("client-1".to_owned(), "hello".to_owned());

        let request = event_with_idempotency(
            "session.message.requested",
            serde_json::json!({"message": "hello"}),
            "session_1",
            "run_1",
            2,
            Some("session.message:session_1:client-1"),
        );
        app.append_events(vec![request], Some(Cursor(2)));

        assert_eq!(
            app.timeline_items(),
            vec![super::TimelineItem::User("hello".to_owned())]
        );
        app.submission_queued("client-1", "evt_2", "session_1");
        assert_eq!(
            app.timeline_items(),
            vec![super::TimelineItem::User("hello".to_owned())]
        );
    }

    #[test]
    fn failed_submission_stays_inline_and_restores_the_draft_for_retry() {
        let mut app = App::connected(
            "0.1".to_owned(),
            vec![session("session_1", "zeta.master", "idle")],
            Vec::new(),
            None,
        );
        let enter = TerminalEvent::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert_eq!(app.handle_event(&enter), AppAction::None);

        app.submission_started("client-1".to_owned(), "try again".to_owned());
        app.submission_failed("client-1", "session is unavailable");
        let backend = TestBackend::new(80, 18);
        let mut terminal = Terminal::new(backend).expect("terminal should initialize");
        terminal
            .draw(|frame| draw(frame, &mut app))
            .expect("failed submission should draw");
        let screen = terminal.backend().to_string();

        assert!(screen.contains("try again"));
        assert!(screen.contains("Failed — session is unavailable"));
        assert!(screen.contains("enter send"));
        assert!(terminal.get_cursor_position().is_ok());
    }

    #[test]
    fn new_submission_shows_starting_state_then_attaches_to_the_created_session() {
        let mut app = App::connected("0.1".to_owned(), Vec::new(), Vec::new(), None);
        app.submission_started("client-1".to_owned(), "build it".to_owned());
        let backend = TestBackend::new(80, 18);
        let mut terminal = Terminal::new(backend).expect("terminal should initialize");
        terminal
            .draw(|frame| draw(frame, &mut app))
            .expect("starting submission should draw");

        assert!(terminal.backend().to_string().contains("Starting session…"));

        app.submission_queued("client-1", "evt_1", "session_new");

        assert_eq!(app.attached_session_id(), Some("session_new"));
        assert_eq!(
            app.timeline_items(),
            vec![
                super::TimelineItem::User("build it".to_owned()),
                super::TimelineItem::Activity {
                    glyph: "·".to_owned(),
                    text: "Queued".to_owned(),
                    color: ratatui::style::Color::Yellow,
                },
            ]
        );
    }

    #[test]
    fn runtime_user_message_folds_into_the_direct_request() {
        let runtime_message = event(
            "zeta.user_message",
            serde_json::json!({"content": "hello"}),
            "session_1",
            "run_1",
            2,
        );
        let request = event(
            "session.message.requested",
            serde_json::json!({"message": "hello"}),
            "session_1",
            "run_1",
            1,
        );
        let app = App::connected(
            "0.1".to_owned(),
            vec![session("session_1", "zeta.master", "running")],
            vec![runtime_message, request],
            Some(Cursor(2)),
        );
        let mut app = app;
        let enter = TerminalEvent::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert_eq!(app.handle_event(&enter), AppAction::None);

        let timeline = app.timeline_events();

        assert_eq!(timeline.len(), 1);
        assert_eq!(timeline[0].event_type(), "session.message.requested");
    }

    #[test]
    fn retry_user_messages_fold_without_a_direct_request() {
        let first = event(
            "zeta.user_message",
            serde_json::json!({"content": "hello"}),
            "session_1",
            "run_1",
            1,
        );
        let retry = event(
            "zeta.user_message",
            serde_json::json!({"content": "hello"}),
            "session_1",
            "run_1",
            2,
        );
        let app = App::connected(
            "0.1".to_owned(),
            vec![session("session_1", "zeta.master", "running")],
            vec![first, retry],
            Some(Cursor(2)),
        );
        let mut app = app;
        let enter = TerminalEvent::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert_eq!(app.handle_event(&enter), AppAction::None);

        assert_eq!(app.timeline_events().len(), 1);
    }

    #[test]
    fn mismatched_or_differently_correlated_messages_remain_distinct() {
        let request = event(
            "session.message.requested",
            serde_json::json!({"message": "hello"}),
            "session_1",
            "run_1",
            1,
        );
        let changed = event(
            "zeta.user_message",
            serde_json::json!({"content": "changed"}),
            "session_1",
            "run_1",
            2,
        );
        let other_run = event(
            "zeta.user_message",
            serde_json::json!({"content": "hello"}),
            "session_1",
            "run_2",
            3,
        );
        let app = App::connected(
            "0.1".to_owned(),
            vec![session("session_1", "zeta.master", "running")],
            vec![request, changed, other_run],
            Some(Cursor(3)),
        );
        let mut app = app;
        let enter = TerminalEvent::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert_eq!(app.handle_event(&enter), AppAction::None);

        assert_eq!(app.timeline_events().len(), 3);
    }

    #[test]
    fn session_list_quits_and_ignores_non_key_events() {
        let mut app = App::connected("0.1".to_owned(), Vec::new(), Vec::new(), None);
        let q = TerminalEvent::Key(KeyEvent::new(KeyCode::Char('q'), KeyModifiers::NONE));

        assert_eq!(app.handle_event(&q), AppAction::Quit);
        assert_eq!(
            app.handle_event(&TerminalEvent::Resize(80, 24)),
            AppAction::None
        );
    }

    #[test]
    fn session_list_navigation_does_not_attach() {
        let first: Session = serde_json::from_value(serde_json::json!({
            "session_id": "session_first",
            "agent_id": "zeta.master",
            "status": "idle"
        }))
        .expect("session should parse");
        let second: Session = serde_json::from_value(serde_json::json!({
            "session_id": "session_second",
            "agent_id": "reviewer",
            "status": "waiting"
        }))
        .expect("session should parse");
        let mut app = App::connected("0.1".to_owned(), vec![first, second], Vec::new(), None);
        let down = TerminalEvent::Key(KeyEvent::new(KeyCode::Down, KeyModifiers::NONE));
        let up = TerminalEvent::Key(KeyEvent::new(KeyCode::Up, KeyModifiers::NONE));

        assert_eq!(app.selected_session_id(), Some("session_first"));
        assert_eq!(app.handle_event(&down), AppAction::None);
        assert_eq!(app.selected_session_id(), Some("session_second"));
        assert_eq!(app.attached_session_id(), None);
        assert_eq!(app.handle_event(&up), AppAction::None);
        assert_eq!(app.selected_session_id(), Some("session_first"));
        assert_eq!(app.attached_session_id(), None);
    }

    #[test]
    fn session_rows_pair_human_status_with_the_latest_activity() {
        let request = event(
            "session.message.requested",
            serde_json::json!({"message": "Inspect the project"}),
            "session_1",
            "run_1",
            1,
        );
        let tool = event(
            "zeta.tool_call.started",
            serde_json::json!({
                "tool_call_id": "call_1",
                "name": "read",
                "input": {"path": "README.md"}
            }),
            "session_1",
            "run_1",
            2,
        );
        let mut app = App::connected(
            "0.1".to_owned(),
            vec![session("session_1", "zeta.master", "running")],
            vec![request, tool],
            Some(Cursor(2)),
        );
        let backend = TestBackend::new(80, 18);
        let mut terminal = Terminal::new(backend).expect("terminal should initialize");
        terminal
            .draw(|frame| draw(frame, &mut app))
            .expect("session list should draw");
        let screen = terminal.backend().to_string();

        assert!(screen.contains("● Running"));
        assert!(screen.contains("read README.md"));
        assert!(!screen.contains("RUNNING"));
    }

    #[test]
    fn completed_tool_activity_shows_exact_duration_and_stays_stable() {
        let started = event(
            "zeta.tool_call.started",
            serde_json::json!({
                "tool_call_id": "call_1",
                "name": "read",
                "input": {"path": "README.md"}
            }),
            "session_1",
            "run_1",
            1,
        );
        let completed = event(
            "zeta.tool_call.completed",
            serde_json::json!({
                "tool_call_id": "call_1",
                "name": "read",
                "result": {"ok": true}
            }),
            "session_1",
            "run_1",
            1_501,
        );
        let mut app = App::connected(
            "0.1".to_owned(),
            vec![session("session_1", "zeta.master", "completed")],
            vec![started, completed],
            Some(Cursor(1_501)),
        );
        let enter = TerminalEvent::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert_eq!(app.handle_event(&enter), AppAction::None);
        let backend = TestBackend::new(80, 18);
        let mut terminal = Terminal::new(backend).expect("terminal should initialize");
        terminal
            .draw(|frame| draw(frame, &mut app))
            .expect("completed activity should draw");
        let before = terminal.backend().to_string();
        assert!(before.contains("✓ read README.md · 1.5s"));

        app.advance_animation();
        terminal
            .draw(|frame| draw(frame, &mut app))
            .expect("completed activity should redraw");
        let after = terminal.backend().to_string();
        assert_eq!(before, after);
    }

    #[test]
    fn active_tool_activity_has_a_subtle_animation_tick() {
        let started = event(
            "zeta.tool_call.started",
            serde_json::json!({
                "tool_call_id": "call_1",
                "name": "read",
                "input": {"path": "README.md"}
            }),
            "session_1",
            "run_1",
            1,
        );
        let mut app = App::connected(
            "0.1".to_owned(),
            vec![session("session_1", "zeta.master", "running")],
            vec![started],
            Some(Cursor(1)),
        );
        let enter = TerminalEvent::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert_eq!(app.handle_event(&enter), AppAction::None);
        let backend = TestBackend::new(80, 18);
        let mut terminal = Terminal::new(backend).expect("terminal should initialize");
        terminal
            .draw(|frame| draw(frame, &mut app))
            .expect("active activity should draw");
        let before = terminal.backend().to_string();
        assert!(before.contains("· read README.md"));

        app.advance_animation();
        terminal
            .draw(|frame| draw(frame, &mut app))
            .expect("active activity should redraw");
        let after = terminal.backend().to_string();
        assert!(after.contains("∙ read README.md"));
        assert_ne!(before, after);
    }

    #[test]
    fn tiny_and_tall_terminals_render_the_same_attached_unicode_draft() {
        let request = event(
            "session.message.requested",
            serde_json::json!({"message": "Inspect 🦀 behavior"}),
            "session_1",
            "run_1",
            1,
        );
        let mut app = App::connected(
            "0.1".to_owned(),
            vec![session("session_1", "zeta.master", "running")],
            vec![request],
            Some(Cursor(1)),
        );
        let enter = TerminalEvent::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        let compose = TerminalEvent::Key(KeyEvent::new(KeyCode::Char('i'), KeyModifiers::NONE));
        assert_eq!(app.handle_event(&enter), AppAction::None);
        assert_eq!(app.handle_event(&compose), AppAction::None);
        assert_eq!(
            app.handle_event(&TerminalEvent::Paste(
                "first 🦀 line\nsecond line".to_owned()
            )),
            AppAction::None
        );
        let backend = TestBackend::new(20, 6);
        let mut terminal = Terminal::new(backend).expect("terminal should initialize");
        terminal
            .draw(|frame| draw(frame, &mut app))
            .expect("tiny terminal should draw");

        terminal.backend_mut().resize(100, 40);
        terminal
            .resize(Rect::new(0, 0, 100, 40))
            .expect("terminal should resize");
        terminal
            .draw(|frame| draw(frame, &mut app))
            .expect("tall terminal should draw");
        let screen = terminal.backend().to_string();

        assert!(screen.contains("Inspect 🦀 behavior"));
        assert!(screen.contains("first 🦀 line"));
        assert!(screen.contains("second line"));
        assert!(screen.contains("enter send"));
        assert!(terminal.get_cursor_position().is_ok());
    }
}
