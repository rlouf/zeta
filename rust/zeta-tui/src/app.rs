use std::collections::HashSet;
use std::io;

use crossterm::event::{Event as TerminalEvent, KeyCode, KeyEventKind};
use crossterm::execute;
use crossterm::terminal::{
    EnterAlternateScreen, LeaveAlternateScreen, disable_raw_mode, enable_raw_mode,
};
use ratatui::Frame;
use ratatui::Terminal;
use ratatui::backend::CrosstermBackend;
use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, List, ListItem, Paragraph};

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
                        }
                    }
                    View::Attached(_) => {
                        if key.code == KeyCode::Esc {
                            self.view = View::Sessions;
                            return AppAction::None;
                        }
                        if key.code == KeyCode::Char('i') {
                            self.mode = Mode::Compose;
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

    fn shows_composer(&self) -> bool {
        match (&self.view, &self.mode) {
            (View::Sessions, Mode::Browse) => false,
            (View::Sessions, Mode::Compose)
            | (View::Attached(_), Mode::Browse)
            | (View::Attached(_), Mode::Compose) => true,
        }
    }

    fn attached_label(&self) -> String {
        let Some(session_id) = self.attached_session_id() else {
            return "Timeline".to_owned();
        };
        for session in &self.sessions {
            if session.session_id() == session_id {
                return session.label();
            }
        }
        session_id.to_owned()
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
    let composer_height = if app.shows_composer() { 3 } else { 0 };
    let areas = Layout::vertical([
        Constraint::Length(1),
        Constraint::Min(3),
        Constraint::Length(composer_height),
        Constraint::Length(1),
    ])
    .split(frame.area());

    let cursor = match app.cursor {
        Some(cursor) => cursor.0.to_string(),
        None => "none".to_owned(),
    };
    let header = Paragraph::new(format!(
        "Zeta  connected · protocol {} · cursor {cursor}",
        app.protocol
    ))
    .style(
        Style::default()
            .fg(Color::Cyan)
            .add_modifier(Modifier::BOLD),
    );
    frame.render_widget(header, areas[0]);

    match &app.view {
        View::Sessions => render_sessions(frame, areas[1], app),
        View::Attached(_) => render_timeline(frame, areas[1], app),
    }

    if app.shows_composer() {
        let title = match &app.view {
            View::Sessions => "New session",
            View::Attached(_) => "Message",
        };
        let composer = Paragraph::new(app.draft.as_str())
            .block(Block::default().title(title).borders(Borders::ALL));
        frame.render_widget(composer, areas[2]);
    }

    let help = match (&app.view, &app.mode) {
        (View::Sessions, Mode::Browse) => "↑/↓ sessions · Enter attach · n new · q quit",
        (View::Sessions, Mode::Compose) => "Enter start · Esc cancel",
        (View::Attached(_), Mode::Browse) => "i compose · Esc detach · q quit",
        (View::Attached(_), Mode::Compose) => "Enter send · Esc cancel",
    };
    frame.render_widget(Paragraph::new(help), areas[3]);
}

fn render_sessions(frame: &mut Frame<'_>, area: Rect, app: &App) {
    let mut rows = Vec::new();
    for (index, session) in app.sessions.iter().enumerate() {
        let marker = if app.selected_session == Some(index) {
            "> "
        } else {
            "  "
        };
        rows.push(ListItem::new(format!("{marker}{}", session.label())));
    }
    if rows.is_empty() {
        rows.push(ListItem::new("No sessions"));
    }
    let sessions = List::new(rows).block(Block::default().title("Sessions").borders(Borders::ALL));
    frame.render_widget(sessions, area);
}

fn render_timeline(frame: &mut Frame<'_>, area: Rect, app: &App) {
    let mut rows = Vec::new();
    for event in app.timeline_events() {
        rows.push(ListItem::new(Line::from(vec![
            Span::styled(event.event_type(), Style::default().fg(Color::Yellow)),
            Span::raw("  "),
            Span::raw(event.timeline_text()),
        ])));
    }
    if rows.is_empty() {
        rows.push(ListItem::new("No events yet"));
    }
    let timeline = List::new(rows).block(
        Block::default()
            .title(app.attached_label())
            .borders(Borders::ALL),
    );
    frame.render_widget(timeline, area);
}

#[cfg(test)]
mod tests {
    use crossterm::event::{Event as TerminalEvent, KeyCode, KeyEvent, KeyModifiers};
    use ratatui::Terminal;
    use ratatui::backend::TestBackend;

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

    #[test]
    fn startup_renders_only_the_session_list() {
        let app = App::connected(
            "0.1".to_owned(),
            vec![session("session_1", "zeta.master", "queued")],
            Vec::new(),
            None,
        );
        let backend = TestBackend::new(80, 18);
        let mut terminal = Terminal::new(backend).expect("terminal should initialize");

        terminal
            .draw(|frame| draw(frame, &app))
            .expect("screen should draw");
        let screen = terminal.backend().to_string();

        assert!(screen.contains("Sessions"));
        assert!(screen.contains("zeta.master · queued"));
        assert!(!screen.contains("Timeline"));
        assert!(!screen.contains("Message"));
        assert!(screen.contains("Enter attach"));
        assert!(screen.contains("n new"));
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
        assert!(screen.contains("zeta.master · running"));
        assert!(screen.contains("selected progress"));
        assert!(!screen.contains("other progress"));
        assert!(!screen.contains("Sessions"));

        assert_eq!(app.handle_event(&escape), AppAction::None);
        terminal
            .draw(|frame| draw(frame, &app))
            .expect("detached screen should draw");
        let screen = terminal.backend().to_string();
        assert!(screen.contains("Sessions"));
        assert!(!screen.contains("selected progress"));
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
