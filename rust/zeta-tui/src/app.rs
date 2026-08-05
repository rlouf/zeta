use std::io;

use crossterm::event::{self, Event as TerminalEvent, KeyCode, KeyEventKind};
use crossterm::execute;
use crossterm::terminal::{
    EnterAlternateScreen, LeaveAlternateScreen, disable_raw_mode, enable_raw_mode,
};
use ratatui::Frame;
use ratatui::Terminal;
use ratatui::backend::CrosstermBackend;
use ratatui::layout::{Constraint, Layout};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, List, ListItem, Paragraph};

use crate::wire::{Cursor, Event};

pub(super) struct App {
    protocol: String,
    events: Vec<Event>,
    cursor: Option<Cursor>,
}

impl App {
    pub(super) fn connected(protocol: String, events: Vec<Event>, cursor: Option<Cursor>) -> Self {
        Self {
            protocol,
            events,
            cursor,
        }
    }
}

pub(super) fn run_terminal(app: &App) -> io::Result<()> {
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
    run_result?;
    raw_result?;
    screen_result?;
    cursor_result
}

fn run_event_loop(
    terminal: &mut Terminal<CrosstermBackend<io::Stdout>>,
    app: &App,
) -> io::Result<()> {
    loop {
        terminal.draw(|frame| draw(frame, app))?;
        let event = event::read()?;
        if should_quit(&event) {
            return Ok(());
        }
    }
}

fn should_quit(event: &TerminalEvent) -> bool {
    let TerminalEvent::Key(key) = event else {
        return false;
    };
    if key.kind != KeyEventKind::Press {
        return false;
    }
    key.code == KeyCode::Char('q') || key.code == KeyCode::Esc
}

pub(super) fn draw(frame: &mut Frame<'_>, app: &App) {
    let areas = Layout::vertical([
        Constraint::Length(1),
        Constraint::Min(3),
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
    frame.render_widget(timeline, areas[1]);

    frame.render_widget(Paragraph::new("q quit"), areas[2]);
}

#[cfg(test)]
mod tests {
    use crossterm::event::{Event as TerminalEvent, KeyCode, KeyEvent, KeyModifiers};
    use ratatui::Terminal;
    use ratatui::backend::TestBackend;

    use super::{App, draw, should_quit};
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
        assert!(screen.contains("Timeline"));
        assert!(screen.contains("zeta.user_message"));
        assert!(screen.contains("hello from Zeta"));
        assert!(screen.contains("q quit"));
    }

    #[test]
    fn quit_keys_exit_but_other_events_do_not() {
        let q = TerminalEvent::Key(KeyEvent::new(KeyCode::Char('q'), KeyModifiers::NONE));
        let escape = TerminalEvent::Key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        let other = TerminalEvent::Key(KeyEvent::new(KeyCode::Char('x'), KeyModifiers::NONE));

        assert!(should_quit(&q));
        assert!(should_quit(&escape));
        assert!(!should_quit(&other));
        assert!(!should_quit(&TerminalEvent::Resize(80, 24)));
    }
}
