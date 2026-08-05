use std::io;

use crossterm::event::{self, Event as TerminalEvent, KeyCode, KeyEventKind};
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

use crate::wire::{Cursor, Event};

pub(super) struct App {
    protocol: String,
    work: Vec<String>,
    events: Vec<Event>,
    cursor: Option<Cursor>,
    mode: Mode,
    draft: String,
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

impl App {
    pub(super) fn connected(protocol: String, events: Vec<Event>, cursor: Option<Cursor>) -> Self {
        let mut work = Vec::new();
        for event in &events {
            let label = event.work_label();
            if !work.contains(&label) {
                work.push(label);
            }
        }
        Self {
            protocol,
            work,
            events,
            cursor,
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
                if key.code == KeyCode::Char('q') || key.code == KeyCode::Esc {
                    return AppAction::Quit;
                }
                if key.code == KeyCode::Char('i') {
                    self.mode = Mode::Compose;
                }
                AppAction::None
            }
            Mode::Compose => {
                if key.code == KeyCode::Esc {
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
            let label = event.work_label();
            if !self.work.contains(&label) {
                self.work.push(label);
            }
            self.events.push(event);
        }
        self.cursor = cursor;
    }
}

pub(super) fn run_terminal(app: &mut App) -> io::Result<AppAction> {
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
    let terminal = Terminal::new(backend);
    let mut terminal = match terminal {
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

    let run_result = run_event_loop(&mut terminal, app);
    let raw_result = disable_raw_mode();
    let screen_result = execute!(terminal.backend_mut(), LeaveAlternateScreen);
    let cursor_result = terminal.show_cursor();
    let action = run_result?;
    raw_result?;
    screen_result?;
    cursor_result?;
    Ok(action)
}

fn run_event_loop(
    terminal: &mut Terminal<CrosstermBackend<io::Stdout>>,
    app: &mut App,
) -> io::Result<AppAction> {
    loop {
        terminal.draw(|frame| draw(frame, app))?;
        let event = event::read()?;
        match app.handle_event(&event) {
            AppAction::None => {}
            AppAction::Quit => return Ok(AppAction::Quit),
            AppAction::Submit(message) => return Ok(AppAction::Submit(message)),
        }
    }
}

pub(super) fn draw(frame: &mut Frame<'_>, app: &App) {
    let areas = Layout::vertical([
        Constraint::Length(1),
        Constraint::Min(3),
        Constraint::Length(3),
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

    if areas[1].width >= 60 {
        let body = Layout::horizontal([Constraint::Length(28), Constraint::Min(1)]).split(areas[1]);
        render_work(frame, body[0], app);
        render_timeline(frame, body[1], app);
    } else {
        let body = Layout::vertical([Constraint::Length(5), Constraint::Min(3)]).split(areas[1]);
        render_work(frame, body[0], app);
        render_timeline(frame, body[1], app);
    }

    let composer = Paragraph::new(app.draft.as_str())
        .block(Block::default().title("Message").borders(Borders::ALL));
    frame.render_widget(composer, areas[2]);

    let help = match app.mode {
        Mode::Browse => "i compose · q quit",
        Mode::Compose => "Enter send · Esc cancel",
    };
    frame.render_widget(Paragraph::new(help), areas[3]);
}

fn render_work(frame: &mut Frame<'_>, area: Rect, app: &App) {
    let mut rows = Vec::new();
    for label in &app.work {
        rows.push(ListItem::new(label.as_str()));
    }
    if rows.is_empty() {
        rows.push(ListItem::new("No correlated work"));
    }
    let work = List::new(rows).block(Block::default().title("Work").borders(Borders::ALL));
    frame.render_widget(work, area);
}

fn render_timeline(frame: &mut Frame<'_>, area: Rect, app: &App) {
    let mut rows = Vec::new();
    for event in &app.events {
        rows.push(ListItem::new(Line::from(vec![
            Span::styled(event.event_type(), Style::default().fg(Color::Yellow)),
            Span::raw("  "),
            Span::raw(event.timeline_text()),
        ])));
    }
    if rows.is_empty() {
        rows.push(ListItem::new("No events yet"));
    }
    let timeline = List::new(rows).block(Block::default().title("Timeline").borders(Borders::ALL));
    frame.render_widget(timeline, area);
}

#[cfg(test)]
mod tests {
    use crossterm::event::{Event as TerminalEvent, KeyCode, KeyEvent, KeyModifiers};
    use ratatui::Terminal;
    use ratatui::backend::TestBackend;

    use super::{App, AppAction, draw};
    use crate::wire::{Cursor, Event};

    #[test]
    fn connected_timeline_renders_current_events() {
        let event: Event = serde_json::from_value(serde_json::json!({
            "id": "evt_123",
            "event_type": "zeta.user_message",
            "source": "user",
            "payload": {"message": "hello from Zeta"},
            "idempotency_key": null,
            "caused_by": null,
            "session_id": "default",
            "run_id": "run_123",
            "turn_id": null,
            "timestamp_ms": 1_754_438_400_000_i64,
            "cursor": 42
        }))
        .expect("event should parse");
        let app = App::connected("0.1".to_owned(), vec![event], Some(Cursor(42)));
        let backend = TestBackend::new(80, 18);
        let mut terminal = Terminal::new(backend).expect("terminal should initialize");

        terminal
            .draw(|frame| draw(frame, &app))
            .expect("screen should draw");
        let screen = terminal.backend().to_string();

        assert!(screen.contains("Zeta  connected · protocol 0.1 · cursor 42"));
        assert!(screen.contains("Work"));
        assert!(screen.contains("default / run_123"));
        assert!(screen.contains("Timeline"));
        assert!(screen.contains("zeta.user_message"));
        assert!(screen.contains("hello from Zeta"));
        assert!(screen.contains("Message"));
        assert!(screen.contains("q quit"));
    }

    #[test]
    fn browse_mode_quits_but_composer_accepts_text() {
        let mut app = App::connected("0.1".to_owned(), Vec::new(), None);
        let q = TerminalEvent::Key(KeyEvent::new(KeyCode::Char('q'), KeyModifiers::NONE));
        let escape = TerminalEvent::Key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        let compose = TerminalEvent::Key(KeyEvent::new(KeyCode::Char('i'), KeyModifiers::NONE));
        let h = TerminalEvent::Key(KeyEvent::new(KeyCode::Char('h'), KeyModifiers::NONE));
        let i = TerminalEvent::Key(KeyEvent::new(KeyCode::Char('i'), KeyModifiers::NONE));
        let enter = TerminalEvent::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));

        assert_eq!(app.handle_event(&compose), AppAction::None);
        assert_eq!(app.handle_event(&h), AppAction::None);
        assert_eq!(app.handle_event(&i), AppAction::None);
        assert_eq!(app.draft, "hi");
        assert_eq!(app.handle_event(&enter), AppAction::Submit("hi".to_owned()));
        assert!(app.draft.is_empty());
        assert_eq!(app.handle_event(&q), AppAction::Quit);
        assert_eq!(app.handle_event(&escape), AppAction::Quit);
        assert_eq!(
            app.handle_event(&TerminalEvent::Resize(80, 24)),
            AppAction::None
        );
    }

    #[test]
    fn narrow_terminal_stacks_work_above_timeline() {
        let app = App::connected("0.1".to_owned(), Vec::new(), None);
        let backend = TestBackend::new(40, 12);
        let mut terminal = Terminal::new(backend).expect("terminal should initialize");

        terminal
            .draw(|frame| draw(frame, &app))
            .expect("screen should draw");
        let screen = terminal.backend().to_string();

        assert!(screen.contains("Work"));
        assert!(screen.contains("No correlated work"));
        assert!(screen.contains("Timeline"));
        assert!(screen.contains("No events yet"));
    }
}
