use std::collections::{HashMap, HashSet};
use std::io;

use crossterm::event::{Event as TerminalEvent, KeyCode, KeyEventKind};
use crossterm::execute;
use crossterm::terminal::{
    EnterAlternateScreen, LeaveAlternateScreen, disable_raw_mode, enable_raw_mode,
};
use ratatui::Frame;
use ratatui::Terminal;
use ratatui::backend::CrosstermBackend;
use ratatui::layout::{Alignment, Constraint, Layout, Margin, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, List, ListItem, ListState, Paragraph};
use serde_json::Value;

use crate::wire::{Cursor, Event, Session};

pub(super) struct App {
    protocol: String,
    sessions: Vec<Session>,
    selected_session: Option<usize>,
    events: Vec<Event>,
    cursor: Option<Cursor>,
    view: View,
    mode: Mode,
    draft: String,
    timeline_mode: TimelineMode,
    timeline_position: Option<usize>,
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

#[derive(Debug, PartialEq, Eq)]
enum TimelineMode {
    Semantic,
    Raw,
}

#[derive(Clone, Debug, PartialEq, Eq)]
enum ToolOutcome {
    Completed,
    Failed(String),
}

#[derive(Debug, PartialEq, Eq)]
enum TimelineItem {
    User(String),
    AgentHeading,
    Agent(String),
    Activity {
        glyph: &'static str,
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
}

impl App {
    pub(super) fn connected(
        protocol: String,
        sessions: Vec<Session>,
        events: Vec<Event>,
        cursor: Option<Cursor>,
    ) -> Self {
        let selected_session = if sessions.is_empty() { None } else { Some(0) };
        Self {
            protocol,
            sessions,
            selected_session,
            events,
            cursor,
            view: View::Sessions,
            mode: Mode::Browse,
            draft: String::new(),
            timeline_mode: TimelineMode::Semantic,
            timeline_position: None,
        }
    }

    pub(super) fn handle_event(&mut self, event: &TerminalEvent) -> AppAction {
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
                            self.view = View::Attached(session_id.to_owned());
                            self.timeline_mode = TimelineMode::Semantic;
                            self.timeline_position = None;
                        }
                    }
                    View::Attached(_) => {
                        if key.code == KeyCode::Esc {
                            self.view = View::Sessions;
                            self.timeline_mode = TimelineMode::Semantic;
                            self.timeline_position = None;
                            return AppAction::None;
                        }
                        if key.code == KeyCode::Char('i') {
                            self.mode = Mode::Compose;
                            return AppAction::None;
                        }
                        if key.code == KeyCode::Char('v') {
                            self.timeline_mode = match self.timeline_mode {
                                TimelineMode::Semantic => TimelineMode::Raw,
                                TimelineMode::Raw => TimelineMode::Semantic,
                            };
                            self.timeline_position = None;
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
                    self.draft.clear();
                    self.mode = Mode::Browse;
                    return AppAction::None;
                }
                if key.code == KeyCode::Enter {
                    let message = self.draft.trim();
                    if message.is_empty() {
                        return AppAction::None;
                    }
                    let message = message.to_owned();
                    self.draft.clear();
                    self.mode = Mode::Browse;
                    return AppAction::Submit(message);
                }
                if key.code == KeyCode::Backspace {
                    self.draft.pop();
                    return AppAction::None;
                }
                if let KeyCode::Char(character) = key.code {
                    self.draft.push(character);
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

    pub(super) fn append_events(&mut self, events: Vec<Event>, cursor: Option<Cursor>) {
        for event in events {
            self.events.push(event);
        }
        self.cursor = cursor;
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

    #[allow(clippy::manual_map)]
    pub(super) fn replace_sessions(&mut self, sessions: Vec<Session>) {
        let selected_id = match self.selected_session_id() {
            Some(session_id) => Some(session_id.to_owned()),
            None => None,
        };
        self.sessions = sessions;
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
        match self.timeline_mode {
            TimelineMode::Raw => raw_timeline_items(&events),
            TimelineMode::Semantic => semantic_timeline_items(&events),
        }
    }

    fn visible_timeline_position(&self, item_count: usize) -> Option<usize> {
        if item_count == 0 {
            return None;
        }
        match self.timeline_position {
            Some(position) => Some(position.min(item_count - 1)),
            None => Some(item_count - 1),
        }
    }

    fn scroll_timeline_up(&mut self) {
        let item_count = self.timeline_items().len();
        let Some(position) = self.visible_timeline_position(item_count) else {
            return;
        };
        self.timeline_position = Some(position.saturating_sub(1));
    }

    fn scroll_timeline_down(&mut self) {
        let item_count = self.timeline_items().len();
        let Some(position) = self.timeline_position else {
            return;
        };
        if position + 1 >= item_count.saturating_sub(1) {
            self.timeline_position = None;
            return;
        }
        self.timeline_position = Some(position + 1);
    }

    fn shows_composer(&self) -> bool {
        match (&self.view, &self.mode) {
            (View::Sessions, Mode::Browse) => false,
            (View::Sessions, Mode::Compose)
            | (View::Attached(_), Mode::Browse)
            | (View::Attached(_), Mode::Compose) => true,
        }
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

fn semantic_timeline_items(events: &[&Event]) -> Vec<TimelineItem> {
    let outcomes = tool_outcomes(events);
    let completed_runs = completed_model_runs(events);
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
            push_activity(&mut items, "·", text, Color::DarkGray);
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
                Some(ToolOutcome::Completed) => {
                    push_activity(&mut items, "✓", description, Color::Green);
                }
                Some(ToolOutcome::Failed(message)) => {
                    let text = format!("{description} failed: {message}");
                    push_activity(&mut items, "×", text, Color::Red);
                }
                None => push_activity(&mut items, "↳", description, Color::DarkGray),
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
            ToolOutcome::Failed(message)
        } else {
            ToolOutcome::Completed
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

fn push_activity(items: &mut Vec<TimelineItem>, glyph: &'static str, text: String, color: Color) {
    items.push(TimelineItem::Activity { glyph, text, color });
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

        let backend = CrosstermBackend::new(output);
        let terminal = match Terminal::new(backend) {
            Ok(terminal) => terminal,
            Err(error) => {
                let raw_result = disable_raw_mode();
                let mut output = io::stdout();
                let screen_result = execute!(output, LeaveAlternateScreen);
                if let Err(restore_error) = raw_result {
                    return Err(io::Error::other(format!(
                        "{error}; raw-mode restoration failed: {restore_error}"
                    )));
                }
                if let Err(restore_error) = screen_result {
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
        })
    }

    pub(super) fn draw(&mut self, app: &App) -> io::Result<()> {
        self.terminal.draw(|frame| draw(frame, app))?;
        Ok(())
    }

    pub(super) fn restore(&mut self) -> io::Result<()> {
        if !self.active {
            return Ok(());
        }
        self.active = false;
        let raw_result = disable_raw_mode();
        let screen_result = execute!(self.terminal.backend_mut(), LeaveAlternateScreen);
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

pub(super) fn draw(frame: &mut Frame<'_>, app: &App) {
    let area = padded_area(frame.area());
    let composer_height = if app.shows_composer() { 3 } else { 0 };
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
        frame.render_widget(empty, areas[1]);
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
        let title = ellipsize(&title, usize::from(areas[1].width.saturating_sub(4)));
        rows.push(ListItem::new(vec![
            Line::from(Span::styled(title, title_style)),
            Line::from(vec![
                Span::styled(
                    session.agent_id().to_owned(),
                    Style::default().fg(Color::DarkGray),
                ),
                Span::raw("  "),
                Span::styled(
                    session.status().to_uppercase(),
                    status_style(session.status()),
                ),
            ]),
            Line::default(),
        ]));
    }
    let sessions = List::new(rows)
        .highlight_symbol("› ")
        .highlight_style(Style::default().add_modifier(Modifier::BOLD));
    let mut state = ListState::default();
    state.select(app.selected_session);
    frame.render_stateful_widget(sessions, areas[1], &mut state);
}

fn render_timeline(frame: &mut Frame<'_>, area: Rect, app: &App) {
    let areas = Layout::vertical([Constraint::Length(1), Constraint::Min(1)]).split(area);
    let area = areas[1];
    let items = app.timeline_items();
    if items.is_empty() {
        frame.render_widget(
            Paragraph::new("Waiting for activity…").style(Style::default().fg(Color::DarkGray)),
            area,
        );
        return;
    }

    let mut rows = Vec::new();
    let width = usize::from(area.width.saturating_sub(2)).max(1);
    for item in items {
        rows.push(timeline_list_item(item, width));
    }
    let item_count = rows.len();
    let mut state = ListState::default();
    state.select(app.visible_timeline_position(item_count));
    frame.render_stateful_widget(List::new(rows), area, &mut state);
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
            left.push(Line::from(vec![
                Span::styled(
                    session.agent_id().to_owned(),
                    Style::default().fg(Color::DarkGray),
                ),
                Span::raw("  "),
                Span::styled(
                    session.status().to_uppercase(),
                    status_style(session.status()),
                ),
            ]));
        }
    }
    frame.render_widget(Paragraph::new(left), areas[0]);

    let right = match app.timeline_mode {
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
    let title = match &app.view {
        View::Sessions => " New session ",
        View::Attached(_) => " Message ",
    };
    let composing = app.mode == Mode::Compose;
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
    let line = if composing {
        Line::from(vec![
            Span::styled("› ", Style::default().fg(Color::Cyan)),
            Span::raw(app.draft.clone()),
        ])
    } else {
        Line::from(Span::styled(
            "› Message Zeta…",
            Style::default().fg(Color::DarkGray),
        ))
    };
    frame.render_widget(Paragraph::new(line).block(block), area);

    if composing {
        let draft_width = u16::try_from(app.draft.chars().count()).unwrap_or(u16::MAX);
        let cursor_x = inner
            .x
            .saturating_add(2)
            .saturating_add(draft_width)
            .min(inner.right().saturating_sub(1));
        frame.set_cursor_position((cursor_x, inner.y));
    }
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
            push_key_hint(&mut spans, "esc", "cancel");
        }
        (View::Attached(_), Mode::Browse) => {
            push_key_hint(&mut spans, "↑/↓", "scroll");
            push_key_hint(&mut spans, "i", "message");
            push_key_hint(&mut spans, "esc", "detach");
            push_key_hint(&mut spans, "v", "raw");
            push_key_hint(&mut spans, "q", "quit");
        }
        (View::Attached(_), Mode::Compose) => {
            push_key_hint(&mut spans, "enter", "send");
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

fn timeline_list_item(item: TimelineItem, width: usize) -> ListItem<'static> {
    match item {
        TimelineItem::User(content) => speaker_item("You", content, width),
        TimelineItem::AgentHeading => speaker_heading("Zeta"),
        TimelineItem::Agent(content) => agent_item(content, width),
        TimelineItem::Activity { glyph, text, color } => activity_item(glyph, text, color, width),
        TimelineItem::Raw {
            event_type,
            payload,
        } => raw_item(event_type, payload, width),
    }
}

fn speaker_item(speaker: &'static str, content: String, width: usize) -> ListItem<'static> {
    let mut lines = vec![Line::from(Span::styled(
        speaker,
        Style::default().add_modifier(Modifier::BOLD),
    ))];
    for line in wrap_text(&content, width.saturating_sub(2).max(1)) {
        lines.push(Line::from(format!("  {line}")));
    }
    lines.push(Line::default());
    ListItem::new(lines)
}

fn speaker_heading(speaker: &'static str) -> ListItem<'static> {
    ListItem::new(Line::from(Span::styled(
        speaker,
        Style::default().add_modifier(Modifier::BOLD),
    )))
}

fn agent_item(content: String, width: usize) -> ListItem<'static> {
    let mut lines = Vec::new();
    for line in wrap_text(&content, width.saturating_sub(2).max(1)) {
        lines.push(Line::from(format!("  {line}")));
    }
    lines.push(Line::default());
    ListItem::new(lines)
}

fn activity_item(
    glyph: &'static str,
    text: String,
    color: Color,
    width: usize,
) -> ListItem<'static> {
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
    ListItem::new(lines)
}

fn raw_item(event_type: String, payload: String, width: usize) -> ListItem<'static> {
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
    ListItem::new(lines)
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
    if status == "running" {
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
        serde_json::from_value(serde_json::json!({
            "id": format!("evt_{cursor}"),
            "event_type": event_type,
            "source": "user",
            "payload": payload,
            "idempotency_key": null,
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
        let app = App::connected(
            "0.1".to_owned(),
            vec![session("session_1", "zeta.master", "queued")],
            vec![request],
            Some(Cursor(1)),
        );
        let backend = TestBackend::new(80, 18);
        let mut terminal = Terminal::new(backend).expect("terminal should initialize");

        terminal
            .draw(|frame| draw(frame, &app))
            .expect("screen should draw");
        let screen = terminal.backend().to_string();

        assert!(screen.contains("Sessions"));
        assert!(screen.contains("Summarize the current project"));
        assert!(screen.contains("zeta.master"));
        assert!(screen.contains("QUEUED"));
        assert!(!screen.contains("Timeline"));
        assert!(!screen.contains("Message"));
        assert!(!screen.contains("protocol 0.1"));
        assert!(!screen.contains("cursor 1"));
        assert!(screen.contains("enter attach"));
        assert!(screen.contains("n new"));
    }

    #[test]
    fn empty_session_list_teaches_the_primary_action() {
        let app = App::connected("0.1".to_owned(), Vec::new(), Vec::new(), None);
        let backend = TestBackend::new(80, 18);
        let mut terminal = Terminal::new(backend).expect("terminal should initialize");

        terminal
            .draw(|frame| draw(frame, &app))
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
        let app = App::connected(
            "0.1".to_owned(),
            vec![session("session_1", "zeta.master", "running")],
            vec![request],
            Some(Cursor(1)),
        );
        let backend = TestBackend::new(40, 10);
        let mut terminal = Terminal::new(backend).expect("terminal should initialize");

        terminal
            .draw(|frame| draw(frame, &app))
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
            .draw(|frame| draw(frame, &app))
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
            .draw(|frame| draw(frame, &app))
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
            .draw(|frame| draw(frame, &app))
            .expect("attached screen should draw");
        let screen = terminal.backend().to_string();
        assert!(screen.contains("zeta.master"));
        assert!(screen.contains("RUNNING"));
        assert!(screen.contains("selected progress"));
        assert!(!screen.contains("other progress"));
        assert!(!screen.contains("Sessions"));

        assert_eq!(app.handle_event(&escape), AppAction::None);
        terminal
            .draw(|frame| draw(frame, &app))
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
}
