use std::cell::RefCell;
use std::collections::{HashMap, HashSet};
use std::env;
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use base64::Engine;
use base64::engine::general_purpose::STANDARD as BASE64;
use crossterm::cursor::Show;
use crossterm::event::{
    DisableBracketedPaste, EnableBracketedPaste, Event as TerminalEvent, KeyCode, KeyEventKind,
    KeyModifiers, KeyboardEnhancementFlags, PopKeyboardEnhancementFlags,
    PushKeyboardEnhancementFlags,
};
use crossterm::execute;
use crossterm::style::Print;
use crossterm::terminal::{
    EnterAlternateScreen, LeaveAlternateScreen, disable_raw_mode, enable_raw_mode,
    supports_keyboard_enhancement,
};
use nucleo_matcher::pattern::{AtomKind, CaseMatching, Normalization, Pattern};
use nucleo_matcher::{Config as MatcherConfig, Matcher, Utf32Str};
use pulldown_cmark::{Event as MarkdownEvent, Options, Parser, Tag, TagEnd};
use ratatui::Frame;
use ratatui::Terminal;
use ratatui::backend::CrosstermBackend;
use ratatui::buffer::Buffer;
use ratatui::layout::{Alignment, Constraint, Layout, Margin, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, List, ListItem, ListState, Paragraph};
use serde_json::Value;
use unicode_width::{UnicodeWidthChar, UnicodeWidthStr};

use crate::wire::{Cursor, Event, Session};

const MAX_CLIPBOARD_BYTES: usize = 100 * 1024;

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
    switcher: Option<SwitcherState>,
    fuzzy_matcher: RefCell<Matcher>,
    connection: ConnectionStatus,
    help: bool,
    feedback: Option<Feedback>,
    terminal_capabilities: TerminalCapabilities,
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

#[derive(Debug, Default, PartialEq, Eq)]
struct SwitcherState {
    query: String,
    selected: usize,
}

#[derive(Clone, Debug, PartialEq, Eq)]
struct SessionMatch {
    index: usize,
    score: u32,
    running: bool,
    latest_activity_ms: i64,
}

#[derive(Clone, Debug, PartialEq, Eq)]
enum ConnectionStatus {
    Connected,
    Reconnecting {
        attempt: usize,
        retry_delay_ms: u64,
        error: String,
    },
}

#[derive(Clone, Debug, PartialEq, Eq)]
struct Feedback {
    glyph: &'static str,
    text: String,
    color: Color,
    remaining_ticks: usize,
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

#[derive(Clone, Debug, PartialEq, Eq)]
struct StyledFragment {
    text: String,
    style: Style,
}

#[derive(Clone, Debug, PartialEq, Eq)]
struct StyledWord {
    text: String,
    style: Style,
    leading_space: bool,
}

#[derive(Debug, PartialEq, Eq)]
struct MarkdownRenderer {
    width: usize,
    lines: Vec<Line<'static>>,
    fragments: Vec<StyledFragment>,
    first_prefix: String,
    continuation_prefix: String,
    lists: Vec<Option<u64>>,
    quote_depth: usize,
    bold_depth: usize,
    italic_depth: usize,
    strikethrough_depth: usize,
    link_depth: usize,
    code_block: bool,
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
    focused_timeline_item: Option<usize>,
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
            focused_timeline_item: None,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) struct TerminalCapabilities {
    color: bool,
    osc8: bool,
    osc52: bool,
}

impl Default for TerminalCapabilities {
    fn default() -> Self {
        Self {
            color: true,
            osc8: false,
            osc52: false,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) enum ClipboardError {
    Unsupported,
    TooLarge,
    WriteFailed,
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

#[derive(Clone, Debug, Hash, PartialEq, Eq)]
struct RunKey(String);

#[derive(Clone, Debug, PartialEq, Eq)]
struct ActiveProgress {
    index: usize,
    event_id: String,
    text: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
enum RunTerminal {
    Completed { event_id: String },
    Failed { event_id: String, message: String },
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
struct RunProgress {
    active: Option<ActiveProgress>,
    completed_tools: Vec<String>,
    completed_started_at_ms: Option<i64>,
    completed_at_ms: Option<i64>,
    last_completion: Option<(usize, String)>,
    failed_tools: Vec<String>,
    last_failure: Option<(usize, String)>,
    terminal: Option<RunTerminal>,
    has_model_output: bool,
}

#[derive(Clone, Debug, PartialEq, Eq)]
struct ProgressProjection {
    glyph: String,
    text: String,
    color: Color,
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

impl TimelineItem {
    fn copy_text(&self) -> Option<&str> {
        match self {
            Self::User(content) | Self::Agent(content) => Some(content),
            Self::Activity { text, .. } => Some(text),
            Self::Raw { payload, .. } => Some(payload),
            Self::AgentHeading => None,
        }
    }
}

#[derive(Debug, PartialEq, Eq)]
pub(super) enum AppAction {
    None,
    Quit,
    Suspend,
    Submit(String),
    Copy(String),
}

pub(super) struct TerminalSession {
    terminal: Terminal<CrosstermBackend<io::Stdout>>,
    active: bool,
    keyboard_enhancement: bool,
    capabilities: TerminalCapabilities,
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

impl MarkdownRenderer {
    fn new(width: usize) -> Self {
        Self {
            width: width.max(1),
            lines: Vec::new(),
            fragments: Vec::new(),
            first_prefix: "  ".to_owned(),
            continuation_prefix: "  ".to_owned(),
            lists: Vec::new(),
            quote_depth: 0,
            bold_depth: 0,
            italic_depth: 0,
            strikethrough_depth: 0,
            link_depth: 0,
            code_block: false,
        }
    }

    fn render(mut self, content: &str) -> Vec<Line<'static>> {
        let parser = Parser::new_ext(content, Options::ENABLE_STRIKETHROUGH);
        for event in parser {
            self.handle_event(event);
        }
        self.flush_prose(false);
        while self.lines.last().is_some_and(|line| line.spans.is_empty()) {
            self.lines.pop();
        }
        self.lines.push(Line::default());
        self.lines
    }

    fn handle_event(&mut self, event: MarkdownEvent<'_>) {
        match event {
            MarkdownEvent::Start(tag) => self.start_tag(tag),
            MarkdownEvent::End(tag) => self.end_tag(tag),
            MarkdownEvent::Text(text) => {
                if self.code_block {
                    self.push_code(&text);
                } else {
                    self.push_text(&text, self.current_style());
                }
            }
            MarkdownEvent::Code(code) => {
                let style = self.current_style().fg(Color::Cyan);
                self.push_text(&code, style);
            }
            MarkdownEvent::InlineMath(math) => {
                let style = self.current_style().fg(Color::Cyan);
                self.push_text(&math, style);
            }
            MarkdownEvent::DisplayMath(math) => {
                self.flush_prose(false);
                self.push_literal_line("    ", &math, Style::default().fg(Color::Cyan));
                self.push_blank();
            }
            MarkdownEvent::Html(html) | MarkdownEvent::InlineHtml(html) => {
                self.push_text(&html, self.current_style());
            }
            MarkdownEvent::FootnoteReference(label) => {
                self.push_text(&format!("[{label}]"), self.current_style());
            }
            MarkdownEvent::SoftBreak => self.push_text(" ", self.current_style()),
            MarkdownEvent::HardBreak => self.flush_prose(false),
            MarkdownEvent::Rule => {
                self.flush_prose(false);
                let rule_width = self.width.saturating_sub(2).min(24);
                self.push_literal_line(
                    "  ",
                    &"─".repeat(rule_width),
                    Style::default().fg(Color::DarkGray),
                );
                self.push_blank();
            }
            MarkdownEvent::TaskListMarker(checked) => {
                let marker = if checked { "☑ " } else { "☐ " };
                self.push_text(marker, self.current_style());
            }
        }
    }

    fn start_tag(&mut self, tag: Tag<'_>) {
        match tag {
            Tag::Paragraph => {}
            Tag::Heading {
                level: _level,
                id: _id,
                classes: _classes,
                attrs: _attrs,
            } => {
                self.flush_prose(false);
                self.bold_depth += 1;
            }
            Tag::BlockQuote(_kind) => {
                self.flush_prose(false);
                self.quote_depth += 1;
                self.set_quote_prefix();
            }
            Tag::CodeBlock(_kind) => {
                self.flush_prose(false);
                self.code_block = true;
            }
            Tag::HtmlBlock => self.flush_prose(false),
            Tag::List(start) => {
                self.flush_prose(false);
                self.lists.push(start);
            }
            Tag::Item => {
                self.flush_prose(false);
                self.set_item_prefix();
            }
            Tag::FootnoteDefinition(_label) => self.flush_prose(false),
            Tag::DefinitionList | Tag::DefinitionListTitle | Tag::DefinitionListDefinition => {
                self.flush_prose(false)
            }
            Tag::Table(_alignments) => self.flush_prose(false),
            Tag::TableHead | Tag::TableRow | Tag::TableCell => self.flush_prose(false),
            Tag::Emphasis => self.italic_depth += 1,
            Tag::Strong => self.bold_depth += 1,
            Tag::Strikethrough => self.strikethrough_depth += 1,
            Tag::Superscript | Tag::Subscript => {}
            Tag::Link {
                link_type: _link_type,
                dest_url: _dest_url,
                title: _title,
                id: _id,
            } => self.link_depth += 1,
            Tag::Image {
                link_type: _link_type,
                dest_url: _dest_url,
                title: _title,
                id: _id,
            } => self.push_text("image: ", Style::default().fg(Color::DarkGray)),
            Tag::MetadataBlock(_kind) => self.flush_prose(false),
        }
    }

    fn end_tag(&mut self, tag: TagEnd) {
        match tag {
            TagEnd::Paragraph => self.flush_prose(self.lists.is_empty()),
            TagEnd::Heading(_level) => {
                self.bold_depth = self.bold_depth.saturating_sub(1);
                self.flush_prose(true);
            }
            TagEnd::BlockQuote(_kind) => {
                self.flush_prose(true);
                self.quote_depth = self.quote_depth.saturating_sub(1);
                self.reset_prefix();
            }
            TagEnd::CodeBlock => {
                self.code_block = false;
                self.push_blank();
            }
            TagEnd::HtmlBlock => self.flush_prose(true),
            TagEnd::List(_ordered) => {
                self.flush_prose(false);
                self.lists.pop();
                self.reset_prefix();
                if self.lists.is_empty() {
                    self.push_blank();
                }
            }
            TagEnd::Item => {
                self.flush_prose(false);
                self.reset_prefix();
            }
            TagEnd::FootnoteDefinition
            | TagEnd::DefinitionList
            | TagEnd::DefinitionListTitle
            | TagEnd::DefinitionListDefinition
            | TagEnd::Table
            | TagEnd::TableHead
            | TagEnd::TableRow
            | TagEnd::TableCell => self.flush_prose(true),
            TagEnd::MetadataBlock(_kind) => self.flush_prose(true),
            TagEnd::Emphasis => self.italic_depth = self.italic_depth.saturating_sub(1),
            TagEnd::Strong => self.bold_depth = self.bold_depth.saturating_sub(1),
            TagEnd::Strikethrough => {
                self.strikethrough_depth = self.strikethrough_depth.saturating_sub(1);
            }
            TagEnd::Superscript | TagEnd::Subscript => {}
            TagEnd::Link => self.link_depth = self.link_depth.saturating_sub(1),
            TagEnd::Image => {}
        }
    }

    fn current_style(&self) -> Style {
        let mut style = Style::default();
        if self.bold_depth > 0 {
            style = style.add_modifier(Modifier::BOLD);
        }
        if self.italic_depth > 0 {
            style = style.add_modifier(Modifier::ITALIC);
        }
        if self.strikethrough_depth > 0 {
            style = style.add_modifier(Modifier::CROSSED_OUT);
        }
        if self.link_depth > 0 {
            style = style.fg(Color::Cyan).add_modifier(Modifier::UNDERLINED);
        }
        style
    }

    fn push_text(&mut self, text: &str, style: Style) {
        let mut parts = text.split('\n').peekable();
        while let Some(part) = parts.next() {
            if !part.is_empty() {
                self.fragments.push(StyledFragment {
                    text: part.to_owned(),
                    style,
                });
            }
            if parts.peek().is_some() {
                self.flush_prose(false);
            }
        }
    }

    fn push_code(&mut self, code: &str) {
        let mut lines = code.split('\n').peekable();
        while let Some(line) = lines.next() {
            if line.is_empty() && lines.peek().is_none() {
                break;
            }
            self.push_literal_line("    ", line, Style::default().fg(Color::Gray));
        }
    }

    fn push_literal_line(&mut self, prefix: &str, text: &str, style: Style) {
        let available = self
            .width
            .saturating_sub(UnicodeWidthStr::width(prefix))
            .max(1);
        let wrapped = wrap_literal(text, available);
        for line in wrapped {
            self.lines.push(Line::from(vec![
                Span::raw(prefix.to_owned()),
                Span::styled(line, style),
            ]));
        }
    }

    fn flush_prose(&mut self, blank_after: bool) {
        if self.fragments.is_empty() {
            if blank_after {
                self.push_blank();
            }
            return;
        }
        let words = styled_words(&self.fragments);
        self.fragments.clear();
        let mut line = Vec::new();
        let mut line_width = UnicodeWidthStr::width(self.first_prefix.as_str());
        let mut prefix = self.first_prefix.clone();
        for StyledWord {
            text,
            style,
            leading_space,
        } in words
        {
            let separator = if leading_space && !line.is_empty() {
                1
            } else {
                0
            };
            let word_width = UnicodeWidthStr::width(text.as_str());
            if !line.is_empty() && line_width + separator + word_width > self.width {
                self.push_word_line(&prefix, line);
                prefix = self.continuation_prefix.clone();
                line = Vec::new();
                line_width = UnicodeWidthStr::width(prefix.as_str());
            }
            if separator > 0 {
                line.push(Span::raw(" "));
                line_width += 1;
            }
            let style = if looks_like_path(&text) {
                style.fg(Color::Cyan)
            } else {
                style
            };
            line.push(Span::styled(text, style));
            line_width += word_width;
        }
        self.push_word_line(&prefix, line);
        if blank_after {
            self.push_blank();
        }
    }

    fn push_word_line(&mut self, prefix: &str, spans: Vec<Span<'static>>) {
        let mut line = vec![Span::raw(prefix.to_owned())];
        line.extend(spans);
        self.lines.push(Line::from(line));
    }

    fn push_blank(&mut self) {
        let blank = match self.lines.last() {
            Some(line) => line.spans.is_empty(),
            None => false,
        };
        if !blank {
            self.lines.push(Line::default());
        }
    }

    fn set_item_prefix(&mut self) {
        let depth = self.lists.len().max(1);
        let indentation = "  ".repeat(depth);
        let marker = match self.lists.last_mut() {
            Some(Some(number)) => {
                let marker = format!("{number}. ");
                *number += 1;
                marker
            }
            Some(None) | None => "• ".to_owned(),
        };
        self.first_prefix = format!("{indentation}{marker}");
        self.continuation_prefix = " ".repeat(UnicodeWidthStr::width(self.first_prefix.as_str()));
    }

    fn set_quote_prefix(&mut self) {
        let indentation = "  ".repeat(self.quote_depth);
        self.first_prefix = format!("{indentation}› ");
        self.continuation_prefix = " ".repeat(UnicodeWidthStr::width(self.first_prefix.as_str()));
    }

    fn reset_prefix(&mut self) {
        if self.quote_depth > 0 {
            self.set_quote_prefix();
            return;
        }
        self.first_prefix = "  ".to_owned();
        self.continuation_prefix = "  ".to_owned();
    }
}

fn wrap_literal(text: &str, width: usize) -> Vec<String> {
    let width = width.max(1);
    let mut lines = Vec::new();
    let mut line = String::new();
    let mut line_width = 0;
    for character in text.chars() {
        let character_width = character.width().unwrap_or(0);
        if line_width > 0 && line_width + character_width > width {
            lines.push(line);
            line = String::new();
            line_width = 0;
        }
        line.push(character);
        line_width += character_width;
    }
    lines.push(line);
    lines
}

fn styled_words(fragments: &[StyledFragment]) -> Vec<StyledWord> {
    let mut words = Vec::new();
    let mut leading_space = false;
    for StyledFragment { text, style } in fragments {
        let mut word = String::new();
        for character in text.chars() {
            if character.is_whitespace() {
                if !word.is_empty() {
                    words.push(StyledWord {
                        text: word,
                        style: *style,
                        leading_space,
                    });
                    word = String::new();
                }
                leading_space = true;
                continue;
            }
            word.push(character);
        }
        if !word.is_empty() {
            words.push(StyledWord {
                text: word,
                style: *style,
                leading_space,
            });
            leading_space = false;
        }
    }
    words
}

fn looks_like_path(text: &str) -> bool {
    let text = text.trim_matches(|character: char| {
        character == ','
            || character == '.'
            || character == ';'
            || character == '('
            || character == ')'
            || character == '['
            || character == ']'
    });
    if text.starts_with("./") || text.starts_with("../") {
        return true;
    }
    if !text.contains('/') || text.contains("://") {
        return false;
    }
    let Some((_, line)) = text.rsplit_once(':') else {
        return text
            .rsplit('/')
            .next()
            .is_some_and(|name| name.contains('.'));
    };
    !line.is_empty() && line.chars().all(|character| character.is_ascii_digit())
}

impl TerminalCapabilities {
    fn detect() -> Self {
        let color = env::var_os("NO_COLOR").is_none()
            && env::var("TERM").map_or(true, |term| term != "dumb")
            && supports_color::on(supports_color::Stream::Stdout)
                .is_some_and(|support| support.has_basic);
        let term_program = env::var("TERM_PROGRAM").unwrap_or_default();
        let term = env::var("TERM").unwrap_or_default();
        let modern_terminal = term_program == "WezTerm"
            || term_program == "iTerm.app"
            || term_program == "ghostty"
            || term.contains("kitty")
            || env::var_os("WT_SESSION").is_some()
            || env::var_os("VTE_VERSION").is_some();
        Self {
            color,
            osc8: modern_terminal,
            osc52: modern_terminal,
        }
    }
}

fn osc52_sequence(text: &str, supported: bool) -> Result<String, ClipboardError> {
    if !supported {
        return Err(ClipboardError::Unsupported);
    }
    if text.len() > MAX_CLIPBOARD_BYTES {
        return Err(ClipboardError::TooLarge);
    }
    Ok(format!("\u{1b}]52;c;{}\u{7}", BASE64.encode(text)))
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
            switcher: None,
            fuzzy_matcher: RefCell::new(Matcher::new(MatcherConfig::DEFAULT)),
            connection: ConnectionStatus::Connected,
            help: false,
            feedback: None,
            terminal_capabilities: TerminalCapabilities::default(),
        }
    }

    pub(super) fn set_terminal_capabilities(&mut self, capabilities: TerminalCapabilities) {
        self.terminal_capabilities = capabilities;
    }

    pub(super) fn copy_succeeded(&mut self) {
        self.feedback = Some(Feedback {
            glyph: "✓",
            text: "Copied".to_owned(),
            color: Color::Green,
            remaining_ticks: 4,
        });
    }

    pub(super) fn copy_failed(&mut self, error: ClipboardError) {
        let text = match error {
            ClipboardError::Unsupported => "Clipboard unavailable",
            ClipboardError::TooLarge => "Selection is too large to copy",
            ClipboardError::WriteFailed => "Could not write to the clipboard",
        };
        self.feedback = Some(Feedback {
            glyph: "×",
            text: text.to_owned(),
            color: Color::Red,
            remaining_ticks: 6,
        });
    }

    pub(super) fn handle_event(&mut self, event: &TerminalEvent) -> AppAction {
        if self.help {
            let TerminalEvent::Key(key) = event else {
                return AppAction::None;
            };
            if key.kind == KeyEventKind::Press
                && (key.code == KeyCode::Esc || key.code == KeyCode::Char('?'))
            {
                self.help = false;
            }
            return AppAction::None;
        }
        if self.switcher.is_some() {
            return self.handle_switcher_event(event);
        }
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
        if key.code == KeyCode::Char('c') && key.modifiers.contains(KeyModifiers::CONTROL) {
            return AppAction::Quit;
        }
        if key.code == KeyCode::Char('z') && key.modifiers.contains(KeyModifiers::CONTROL) {
            return AppAction::Suspend;
        }

        match self.mode {
            Mode::Browse => {
                if key.code == KeyCode::Char('q') {
                    return AppAction::Quit;
                }
                if key.code == KeyCode::Char('/') {
                    self.switcher = Some(SwitcherState::default());
                    return AppAction::None;
                }
                if key.code == KeyCode::Char('?') {
                    self.help = true;
                    return AppAction::None;
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
                        if let KeyCode::Char(character) = key.code
                            && let Some(position) = numbered_position(character)
                        {
                            self.attach_session_at(position);
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
                        if key.code == KeyCode::Tab {
                            self.focus_next_timeline_item();
                            return AppAction::None;
                        }
                        if key.code == KeyCode::BackTab {
                            self.focus_previous_timeline_item();
                            return AppAction::None;
                        }
                        if key.code == KeyCode::Char('y') {
                            let Some(content) = self.focused_timeline_content() else {
                                return AppAction::None;
                            };
                            return AppAction::Copy(content);
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

    fn handle_switcher_event(&mut self, event: &TerminalEvent) -> AppAction {
        if let TerminalEvent::Paste(text) = event {
            let Some(switcher) = &mut self.switcher else {
                return AppAction::None;
            };
            switcher.query.push_str(text);
            switcher.selected = 0;
            return AppAction::None;
        }
        let TerminalEvent::Key(key) = event else {
            return AppAction::None;
        };
        if key.kind != KeyEventKind::Press {
            return AppAction::None;
        }
        if key.code == KeyCode::Esc {
            self.switcher = None;
            return AppAction::None;
        }
        if key.code == KeyCode::Enter {
            let session_id = self.switcher_selected_session_id();
            let Some(session_id) = session_id else {
                return AppAction::None;
            };
            self.switcher = None;
            self.attach_session(session_id);
            return AppAction::None;
        }
        if key.code == KeyCode::Down || key.code == KeyCode::Tab {
            let match_count = self.switcher_matches().len();
            let Some(switcher) = &mut self.switcher else {
                return AppAction::None;
            };
            if match_count > 0 {
                switcher.selected = (switcher.selected + 1).min(match_count - 1);
            }
            return AppAction::None;
        }
        if key.code == KeyCode::Up || key.code == KeyCode::BackTab {
            let Some(switcher) = &mut self.switcher else {
                return AppAction::None;
            };
            switcher.selected = switcher.selected.saturating_sub(1);
            return AppAction::None;
        }
        if key.code == KeyCode::Backspace {
            let Some(switcher) = &mut self.switcher else {
                return AppAction::None;
            };
            switcher.query.pop();
            switcher.selected = 0;
            return AppAction::None;
        }
        if let KeyCode::Char(character) = key.code
            && let Some(position) = numbered_position(character)
        {
            let matches = self.switcher_matches();
            let Some(session_match) = matches.get(position) else {
                return AppAction::None;
            };
            let session_id = self.sessions[session_match.index].session_id().to_owned();
            self.switcher = None;
            self.attach_session(session_id);
            return AppAction::None;
        }
        if let KeyCode::Char(character) = key.code
            && !key.modifiers.contains(KeyModifiers::CONTROL)
            && !key.modifiers.contains(KeyModifiers::SUPER)
        {
            let Some(switcher) = &mut self.switcher else {
                return AppAction::None;
            };
            switcher.query.push(character);
            switcher.selected = 0;
        }
        AppAction::None
    }

    pub(super) fn cursor(&self) -> Option<u64> {
        if let Some(cursor) = self.cursor {
            return Some(cursor.0);
        }
        None
    }

    pub(super) fn advance_animation(&mut self) {
        self.animation_frame = (self.animation_frame + 1) % 4;
        let Some(feedback) = &mut self.feedback else {
            return;
        };
        feedback.remaining_ticks = feedback.remaining_ticks.saturating_sub(1);
        if feedback.remaining_ticks == 0 {
            self.feedback = None;
        }
    }

    pub(super) fn set_keyboard_enhancement(&mut self, supported: bool) {
        self.keyboard_enhancement = supported;
    }

    pub(super) fn set_connected(&mut self) {
        self.connection = ConnectionStatus::Connected;
    }

    pub(super) fn set_protocol(&mut self, protocol: String) {
        self.protocol = protocol;
    }

    pub(super) fn set_reconnecting(&mut self, attempt: usize, retry_delay_ms: u64, error: String) {
        self.connection = ConnectionStatus::Reconnecting {
            attempt,
            retry_delay_ms,
            error: single_line(&error),
        };
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
        self.feedback = Some(Feedback {
            glyph: "✓",
            text: "Sent".to_owned(),
            color: Color::Green,
            remaining_ticks: 4,
        });
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
        self.feedback = Some(Feedback {
            glyph: "↺",
            text: "Draft restored for retry".to_owned(),
            color: Color::Yellow,
            remaining_ticks: 6,
        });
    }

    pub(super) fn submission_for_replay(&self, id: &str) -> Option<(Option<String>, String)> {
        let submission = self
            .submissions
            .iter()
            .find(|submission| submission.id.0 == id)?;
        let session_id = match &submission.target {
            SubmissionTarget::NewSession => None,
            SubmissionTarget::Session(session_id) => Some(session_id.clone()),
        };
        Some((session_id, submission.message.clone()))
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

    fn attach_session_at(&mut self, position: usize) {
        let Some(session) = self.sessions.get(position) else {
            return;
        };
        self.selected_session = Some(position);
        self.attach_session(session.session_id().to_owned());
    }

    fn switcher_selected_session_id(&self) -> Option<String> {
        let switcher = self.switcher.as_ref()?;
        let matches = self.switcher_matches();
        let session_match = matches.get(switcher.selected)?;
        Some(self.sessions[session_match.index].session_id().to_owned())
    }

    fn switcher_matches(&self) -> Vec<SessionMatch> {
        let query = match &self.switcher {
            Some(switcher) => switcher.query.as_str(),
            None => "",
        };
        let pattern = Pattern::new(
            query,
            CaseMatching::Ignore,
            Normalization::Smart,
            AtomKind::Fuzzy,
        );
        let mut matcher = self.fuzzy_matcher.borrow_mut();
        let mut buffer = Vec::new();
        let mut matches = Vec::new();
        for (index, session) in self.sessions.iter().enumerate() {
            let title = self.session_title(session.session_id());
            let activity = self
                .session_activity(session.session_id())
                .unwrap_or_default();
            let haystack = format!(
                "{title} {} {} {activity}",
                session.agent_id(),
                session.status()
            );
            let Some(score) = pattern.score(Utf32Str::new(&haystack, &mut buffer), &mut matcher)
            else {
                continue;
            };
            matches.push(SessionMatch {
                index,
                score,
                running: session.status() == "running",
                latest_activity_ms: self.latest_session_activity_ms(session.session_id()),
            });
        }
        matches.sort_by(|left, right| {
            right
                .running
                .cmp(&left.running)
                .then_with(|| right.score.cmp(&left.score))
                .then_with(|| right.latest_activity_ms.cmp(&left.latest_activity_ms))
                .then_with(|| left.index.cmp(&right.index))
        });
        matches
    }

    fn latest_session_activity_ms(&self, session_id: &str) -> i64 {
        let mut latest = 0;
        for event in &self.events {
            if event.belongs_to_session(session_id) {
                latest = latest.max(event.timestamp_ms());
            }
        }
        latest
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
        let selected_id = match &self.view {
            View::Attached(session_id) => Some(session_id.clone()),
            View::Sessions => match self.selected_session_id() {
                Some(session_id) => Some(session_id.to_owned()),
                None => None,
            },
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
                    push_activity(&mut items, "×", format!("Failed: {error}"), Color::Red)
                }
            }
        }
        items
    }

    fn focus_next_timeline_item(&mut self) {
        self.move_timeline_focus(false);
    }

    fn focus_previous_timeline_item(&mut self) {
        self.move_timeline_focus(true);
    }

    fn move_timeline_focus(&mut self, backwards: bool) {
        let items = self.timeline_items();
        let mut copyable = Vec::new();
        for (index, item) in items.iter().enumerate() {
            if item.copy_text().is_some() {
                copyable.push(index);
            }
        }
        if copyable.is_empty() {
            self.view_state_mut().focused_timeline_item = None;
            return;
        }
        let current = self.view_state().focused_timeline_item;
        let selected = match current
            .and_then(|current| copyable.iter().position(|candidate| *candidate == current))
        {
            Some(position) if backwards && position == 0 => copyable.len() - 1,
            Some(position) if backwards => position - 1,
            Some(position) => (position + 1) % copyable.len(),
            None if backwards => copyable.len() - 1,
            None => 0,
        };
        self.view_state_mut().focused_timeline_item = Some(copyable[selected]);
    }

    fn focused_timeline_content(&mut self) -> Option<String> {
        let items = self.timeline_items();
        let focused = match self.view_state().focused_timeline_item {
            Some(focused)
                if items
                    .get(focused)
                    .and_then(TimelineItem::copy_text)
                    .is_some() =>
            {
                focused
            }
            Some(_) | None => {
                let focused = items
                    .iter()
                    .enumerate()
                    .rev()
                    .find_map(|(index, item)| item.copy_text().map(|_| index))?;
                self.view_state_mut().focused_timeline_item = Some(focused);
                focused
            }
        };
        items[focused].copy_text().map(str::to_owned)
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
        if self.switcher.is_some() || self.help {
            return false;
        }
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
    let completed_runs = completed_model_runs(events);
    let progress = progress_projections(events, animation_frame);
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
            push_progress_projection(event, &progress, &mut items, &mut agent_started);
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
            push_progress_projection(event, &progress, &mut items, &mut agent_started);
            continue;
        }
        if event_type == "zeta.tool_call.completed" || event_type == "zeta.tool_call.failed" {
            push_progress_projection(event, &progress, &mut items, &mut agent_started);
            continue;
        }
        if event_type == "zeta.turn.completed" {
            push_progress_projection(event, &progress, &mut items, &mut agent_started);
            continue;
        }
        if event_type == "zeta.turn.failed" {
            push_progress_projection(event, &progress, &mut items, &mut agent_started);
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

fn push_progress_projection(
    event: &Event,
    progress: &HashMap<String, ProgressProjection>,
    items: &mut Vec<TimelineItem>,
    agent_started: &mut bool,
) {
    let Some(projection) = progress.get(event.id()) else {
        return;
    };
    start_agent_timeline(items, agent_started);
    push_activity(
        items,
        &projection.glyph,
        projection.text.clone(),
        projection.color,
    );
}

fn progress_projections(
    events: &[&Event],
    animation_frame: usize,
) -> HashMap<String, ProgressProjection> {
    let mut outcomes = HashMap::new();
    for (index, event) in events.iter().enumerate() {
        if event.event_type() != "zeta.tool_call.completed"
            && event.event_type() != "zeta.tool_call.failed"
        {
            continue;
        }
        let Some(tool_call_id) = payload_string(event, "tool_call_id") else {
            continue;
        };
        outcomes.insert(tool_call_id.to_owned(), (index, *event));
    }

    let mut runs = HashMap::<RunKey, RunProgress>::new();
    let mut paired_tool_calls = HashSet::new();
    for (index, event) in events.iter().enumerate() {
        let key = run_key(event);
        let run = runs.entry(key).or_default();
        let event_type = event.event_type();
        if event_type == "runtime.status.update" {
            let status = payload_string(event, "status").unwrap_or("working");
            let text = if status == "reasoning_delta" {
                "Thinking…".to_owned()
            } else {
                match payload_string(event, "text") {
                    Some(text) => format!("{}…", single_line(text).trim_end_matches('…')),
                    None => format!("{}…", humanize(status)),
                }
            };
            run.active = Some(ActiveProgress {
                index,
                event_id: event.id().to_owned(),
                text,
            });
            continue;
        }
        if event_type == "zeta.tool_call.started" {
            let description = tool_description(event);
            let Some(tool_call_id) = payload_string(event, "tool_call_id") else {
                run.active = Some(ActiveProgress {
                    index,
                    event_id: event.id().to_owned(),
                    text: active_tool_description(&description),
                });
                continue;
            };
            let Some((outcome_index, outcome)) = outcomes.get(tool_call_id) else {
                run.active = Some(ActiveProgress {
                    index,
                    event_id: event.id().to_owned(),
                    text: active_tool_description(&description),
                });
                continue;
            };
            paired_tool_calls.insert(tool_call_id.to_owned());
            if outcome.event_type() == "zeta.tool_call.failed" {
                record_tool_failure(run, *outcome_index, outcome, &description);
            } else {
                record_tool_completion(run, *outcome_index, event, outcome, description);
            }
            continue;
        }
        if event_type == "zeta.tool_call.completed" || event_type == "zeta.tool_call.failed" {
            let paired = match payload_string(event, "tool_call_id") {
                Some(tool_call_id) => paired_tool_calls.contains(tool_call_id),
                None => false,
            };
            if paired {
                continue;
            }
            let description = tool_description(event);
            if event_type == "zeta.tool_call.failed" {
                record_tool_failure(run, index, event, &description);
            } else {
                run.completed_tools.push(description);
                run.completed_at_ms = Some(event.timestamp_ms());
                run.last_completion = Some((index, event.id().to_owned()));
            }
            continue;
        }
        if event_type == "zeta.model_call.completed" {
            let has_content = match payload_string(event, "content") {
                Some(content) => !content.trim().is_empty(),
                None => false,
            };
            run.has_model_output |= has_content;
            continue;
        }
        if event_type == "zeta.turn.completed" {
            run.terminal = Some(RunTerminal::Completed {
                event_id: event.id().to_owned(),
            });
            continue;
        }
        if event_type == "zeta.turn.failed" {
            let message = payload_string(event, "content")
                .or_else(|| payload_string(event, "reason"))
                .unwrap_or("Turn failed");
            run.terminal = Some(RunTerminal::Failed {
                event_id: event.id().to_owned(),
                message: single_line(message),
            });
        }
    }

    let mut projections = HashMap::new();
    for (_, run) in runs {
        let projection = run_progress_projection(&run, animation_frame);
        let Some((event_id, projection)) = projection else {
            continue;
        };
        projections.insert(event_id, projection);
    }
    projections
}

fn run_progress_projection(
    run: &RunProgress,
    animation_frame: usize,
) -> Option<(String, ProgressProjection)> {
    if let Some(terminal) = &run.terminal {
        match terminal {
            RunTerminal::Failed { event_id, message } => {
                return Some((
                    event_id.clone(),
                    ProgressProjection {
                        glyph: "×".to_owned(),
                        text: message.clone(),
                        color: Color::Red,
                    },
                ));
            }
            RunTerminal::Completed { .. } => {}
        }
    }
    if let Some((_, event_id)) = &run.last_failure {
        let failure_count = run.failed_tools.len();
        let text = if failure_count == 1 {
            run.failed_tools[0].clone()
        } else {
            format!(
                "{failure_count} tools failed: {}",
                run.failed_tools[failure_count - 1]
            )
        };
        return Some((
            event_id.clone(),
            ProgressProjection {
                glyph: "×".to_owned(),
                text,
                color: Color::Red,
            },
        ));
    }
    let completion_index = run.last_completion.as_ref().map(|(index, _)| *index);
    if run.terminal.is_none()
        && !run.has_model_output
        && let Some(active) = &run.active
    {
        let is_latest = match completion_index {
            Some(index) => active.index > index,
            None => true,
        };
        if is_latest {
            return Some((
                active.event_id.clone(),
                ProgressProjection {
                    glyph: active_glyph(animation_frame).to_owned(),
                    text: active.text.clone(),
                    color: Color::DarkGray,
                },
            ));
        }
    }
    if let Some((_, event_id)) = &run.last_completion {
        let mut text = completed_tools_summary(&run.completed_tools);
        if let (Some(started_at_ms), Some(completed_at_ms)) =
            (run.completed_started_at_ms, run.completed_at_ms)
        {
            text = with_duration(text, started_at_ms, completed_at_ms);
        }
        return Some((
            event_id.clone(),
            ProgressProjection {
                glyph: "✓".to_owned(),
                text,
                color: Color::Green,
            },
        ));
    }
    if let Some(RunTerminal::Completed { event_id }) = &run.terminal
        && !run.has_model_output
    {
        return Some((
            event_id.clone(),
            ProgressProjection {
                glyph: "✓".to_owned(),
                text: "Completed".to_owned(),
                color: Color::Green,
            },
        ));
    }
    None
}

fn record_tool_completion(
    run: &mut RunProgress,
    outcome_index: usize,
    started: &Event,
    completed: &Event,
    description: String,
) {
    run.completed_tools.push(description);
    run.completed_started_at_ms = match run.completed_started_at_ms {
        Some(timestamp_ms) => Some(timestamp_ms.min(started.timestamp_ms())),
        None => Some(started.timestamp_ms()),
    };
    run.completed_at_ms = match run.completed_at_ms {
        Some(timestamp_ms) => Some(timestamp_ms.max(completed.timestamp_ms())),
        None => Some(completed.timestamp_ms()),
    };
    let is_latest = match &run.last_completion {
        Some((index, _)) => outcome_index > *index,
        None => true,
    };
    if is_latest {
        run.last_completion = Some((outcome_index, completed.id().to_owned()));
    }
}

fn record_tool_failure(
    run: &mut RunProgress,
    outcome_index: usize,
    event: &Event,
    description: &str,
) {
    let message = event_error(event).unwrap_or_else(|| "unknown error".to_owned());
    run.failed_tools
        .push(format!("{description} failed: {message}"));
    let is_latest = match &run.last_failure {
        Some((index, _)) => outcome_index > *index,
        None => true,
    };
    if is_latest {
        run.last_failure = Some((outcome_index, event.id().to_owned()));
    }
}

fn run_key(event: &Event) -> RunKey {
    match event.run_id() {
        Some(run_id) => RunKey(run_id.to_owned()),
        None => RunKey(event.id().to_owned()),
    }
}

fn active_tool_description(description: &str) -> String {
    let Some((verb, detail)) = description.split_once(' ') else {
        return "Working…".to_owned();
    };
    let action = match verb.to_ascii_lowercase().as_str() {
        "read" => "Reading",
        "search" => "Searching",
        "run" => "Running",
        "write" => "Writing",
        "edit" => "Editing",
        "list" => "Listing",
        _ => "Using",
    };
    format!("{action} {detail}…")
}

fn completed_tools_summary(descriptions: &[String]) -> String {
    if descriptions.len() > 1
        && descriptions
            .iter()
            .all(|description| description.to_ascii_lowercase().starts_with("read "))
    {
        return format!("Inspected {} files", descriptions.len());
    }
    if descriptions.len() == 1 {
        return capitalize_first(&descriptions[0]);
    }
    format!("Completed {} tools", descriptions.len())
}

fn capitalize_first(text: &str) -> String {
    let mut characters = text.chars();
    let Some(first) = characters.next() else {
        return String::new();
    };
    format!("{}{}", first.to_uppercase(), characters.as_str())
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

fn numbered_position(character: char) -> Option<usize> {
    let number = character.to_digit(10)?;
    if number == 0 {
        return None;
    }
    usize::try_from(number - 1).ok()
}

fn relative_activity(timestamp_ms: i64) -> String {
    if timestamp_ms <= 0 {
        return "no activity".to_owned();
    }
    let now_ms = match SystemTime::now().duration_since(UNIX_EPOCH) {
        Ok(duration) => i64::try_from(duration.as_millis()).unwrap_or(i64::MAX),
        Err(_error) => timestamp_ms,
    };
    let seconds = now_ms.saturating_sub(timestamp_ms) / 1_000;
    if seconds == 0 {
        return "now".to_owned();
    }
    if seconds < 60 {
        return format!("{seconds}s");
    }
    let minutes = seconds / 60;
    if minutes < 60 {
        return format!("{minutes}m");
    }
    let hours = minutes / 60;
    if hours < 24 {
        return format!("{hours}h");
    }
    format!("{}d", hours / 24)
}

impl TerminalSession {
    pub(super) fn start() -> io::Result<Self> {
        let capabilities = TerminalCapabilities::detect();
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
            capabilities,
        })
    }

    pub(super) fn keyboard_enhancement(&self) -> bool {
        self.keyboard_enhancement
    }

    pub(super) fn capabilities(&self) -> TerminalCapabilities {
        self.capabilities
    }

    pub(super) fn copy_to_clipboard(&mut self, text: &str) -> Result<(), ClipboardError> {
        let sequence = osc52_sequence(text, self.capabilities.osc52)?;
        execute!(self.terminal.backend_mut(), Print(sequence))
            .map_err(|_error| ClipboardError::WriteFailed)?;
        self.terminal
            .backend_mut()
            .flush()
            .map_err(|_error| ClipboardError::WriteFailed)
    }

    pub(super) fn install_panic_hook(&self) {
        let previous = std::panic::take_hook();
        let keyboard_enhancement = self.keyboard_enhancement;
        std::panic::set_hook(Box::new(move |panic_info| {
            let _ = disable_raw_mode();
            let mut output = io::stdout();
            if keyboard_enhancement {
                let _ = execute!(
                    output,
                    DisableBracketedPaste,
                    PopKeyboardEnhancementFlags,
                    LeaveAlternateScreen,
                    Show
                );
            } else {
                let _ = execute!(output, DisableBracketedPaste, LeaveAlternateScreen, Show);
            }
            previous(panic_info);
        }));
    }

    pub(super) fn resume(&mut self) -> io::Result<()> {
        if self.active {
            return Ok(());
        }
        enable_raw_mode()?;
        self.active = true;
        let result = if self.keyboard_enhancement {
            execute!(
                self.terminal.backend_mut(),
                EnterAlternateScreen,
                PushKeyboardEnhancementFlags(
                    KeyboardEnhancementFlags::DISAMBIGUATE_ESCAPE_CODES
                        | KeyboardEnhancementFlags::REPORT_ALL_KEYS_AS_ESCAPE_CODES
                        | KeyboardEnhancementFlags::REPORT_ALTERNATE_KEYS
                        | KeyboardEnhancementFlags::REPORT_EVENT_TYPES
                ),
                EnableBracketedPaste
            )
        } else {
            execute!(
                self.terminal.backend_mut(),
                EnterAlternateScreen,
                EnableBracketedPaste
            )
        };
        if let Err(error) = result {
            let _ = self.restore();
            return Err(error);
        }
        self.terminal.clear()?;
        Ok(())
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

    if app.help {
        render_help(frame, areas[1], app);
    } else if app.switcher.is_some() {
        render_switcher(frame, areas[1], app);
    } else {
        match &app.view {
            View::Sessions => render_sessions(frame, areas[1], app),
            View::Attached(_) => render_timeline(frame, areas[1], app),
        }
    }

    if app.shows_composer() {
        render_composer(frame, areas[2], app);
    }

    frame.render_widget(Paragraph::new(footer_line(app)), areas[3]);
    apply_terminal_capabilities(frame.buffer_mut(), app.terminal_capabilities);
}

fn apply_terminal_capabilities(buffer: &mut Buffer, capabilities: TerminalCapabilities) {
    if capabilities.osc8 {
        apply_local_file_links(buffer);
    }
    if !capabilities.color {
        for cell in &mut buffer.content {
            cell.set_fg(Color::Reset);
            cell.set_bg(Color::Reset);
        }
    }
}

fn apply_local_file_links(buffer: &mut Buffer) {
    let area = buffer.area;
    for y in area.top()..area.bottom() {
        let mut start = None;
        for x in area.left()..=area.right() {
            let character = if x == area.right() {
                ' '
            } else {
                buffer[(x, y)].symbol().chars().next().unwrap_or(' ')
            };
            if character.is_ascii_whitespace() {
                if let Some(token_start) = start.take() {
                    link_local_file_token(buffer, token_start, x, y);
                }
            } else if start.is_none() {
                start = Some(x);
            }
        }
    }
}

fn link_local_file_token(buffer: &mut Buffer, start: u16, end: u16, y: u16) {
    let mut token = String::new();
    for x in start..end {
        token.push_str(buffer[(x, y)].symbol());
    }
    if !token.is_ascii() || token.chars().any(char::is_control) {
        return;
    }
    let trimmed_start = token.trim_start_matches(['`', '(', '[']);
    let prefix_width = token.len().saturating_sub(trimmed_start.len());
    let token = trimmed_start.trim_end_matches(|character| {
        character == '`'
            || character == ','
            || character == ';'
            || character == ')'
            || character == ']'
    });
    let Some(path) = local_file_path(token) else {
        return;
    };
    let Ok(path) = path.canonicalize() else {
        return;
    };
    let url = format!("file://{}", escape_file_url(&path));
    let visible = token.as_bytes();
    let token_start = start.saturating_add(u16::try_from(prefix_width).unwrap_or(u16::MAX));
    for (index, chunk) in visible.chunks(2).enumerate() {
        let Ok(offset) = u16::try_from(index.saturating_mul(2)) else {
            break;
        };
        let x = token_start.saturating_add(offset);
        if x >= end {
            break;
        }
        let Ok(text) = std::str::from_utf8(chunk) else {
            return;
        };
        let hyperlink = format!("\u{1b}]8;;{url}\u{7}{text}\u{1b}]8;;\u{7}");
        buffer[(x, y)].set_symbol(&hyperlink);
    }
}

fn local_file_path(token: &str) -> Option<PathBuf> {
    let path_text = match token.rsplit_once(':') {
        Some((path, line)) if line.chars().all(|character| character.is_ascii_digit()) => path,
        Some(_) | None => token,
    };
    if !looks_like_path(path_text) {
        return None;
    }
    let path = Path::new(path_text);
    if path.is_file() {
        return Some(path.to_owned());
    }
    None
}

fn escape_file_url(path: &Path) -> String {
    path.to_string_lossy()
        .replace('%', "%25")
        .replace(' ', "%20")
        .replace('#', "%23")
        .replace('?', "%3F")
}

fn render_help(frame: &mut Frame<'_>, area: Rect, app: &App) {
    let mut lines = vec![Line::from(Span::styled(
        match &app.view {
            View::Sessions => "Session keys",
            View::Attached(_) => "Conversation keys",
        },
        Style::default().add_modifier(Modifier::BOLD),
    ))];
    lines.push(Line::default());
    match &app.view {
        View::Sessions => {
            push_help_key(&mut lines, "↑/↓ or j/k", "Move selection");
            push_help_key(&mut lines, "enter", "Attach to selected session");
            push_help_key(&mut lines, "1–9", "Attach directly");
            push_help_key(&mut lines, "/", "Switch sessions");
            push_help_key(&mut lines, "n", "Start a new session");
            push_help_key(&mut lines, "q", "Quit");
        }
        View::Attached(_) => {
            push_help_key(&mut lines, "↑/↓ or j/k", "Scroll one line");
            push_help_key(&mut lines, "pgup/pgdn", "Scroll one page");
            push_help_key(&mut lines, "g / G", "Jump to top / live output");
            push_help_key(&mut lines, "tab / shift-tab", "Focus transcript item");
            push_help_key(&mut lines, "y", "Copy focused item");
            push_help_key(&mut lines, "i", "Message Zeta");
            push_help_key(&mut lines, "/", "Switch sessions");
            push_help_key(&mut lines, "v", "Toggle raw events");
            push_help_key(&mut lines, "esc", "Detach to sessions");
            push_help_key(&mut lines, "q", "Quit");
        }
    }
    lines.push(Line::default());
    push_help_key(&mut lines, "?", "Close help");
    frame.render_widget(Paragraph::new(lines), area);
}

fn push_help_key(lines: &mut Vec<Line<'static>>, key: &'static str, label: &'static str) {
    lines.push(Line::from(vec![
        Span::styled(
            format!("{key:<12}"),
            Style::default()
                .fg(Color::Cyan)
                .add_modifier(Modifier::BOLD),
        ),
        Span::styled(label, Style::default().fg(Color::DarkGray)),
    ]));
}

fn render_switcher(frame: &mut Frame<'_>, area: Rect, app: &App) {
    let areas = Layout::vertical([Constraint::Length(3), Constraint::Min(1)]).split(area);
    let switcher = app
        .switcher
        .as_ref()
        .expect("switcher view owns switcher state");
    let input = Paragraph::new(vec![
        Line::from(Span::styled(
            "Switch sessions",
            Style::default().add_modifier(Modifier::BOLD),
        )),
        Line::from(vec![
            Span::styled("/ ", Style::default().fg(Color::Cyan)),
            Span::raw(switcher.query.clone()),
        ]),
    ]);
    frame.render_widget(input, areas[0]);
    let cursor_column = 2_u16.saturating_add(
        u16::try_from(UnicodeWidthStr::width(switcher.query.as_str())).unwrap_or(u16::MAX),
    );
    frame.set_cursor_position((
        areas[0]
            .x
            .saturating_add(cursor_column)
            .min(areas[0].right().saturating_sub(1)),
        areas[0].y.saturating_add(1),
    ));

    let matches = app.switcher_matches();
    if matches.is_empty() {
        frame.render_widget(
            Paragraph::new(vec![
                Line::from(Span::styled(
                    "No matching sessions",
                    Style::default().add_modifier(Modifier::BOLD),
                )),
                Line::from(Span::styled(
                    "Try a title, status, agent, or recent activity.",
                    Style::default().fg(Color::DarkGray),
                )),
            ]),
            areas[1],
        );
        return;
    }

    let mut rows = Vec::new();
    for (position, session_match) in matches.iter().enumerate() {
        let session = &app.sessions[session_match.index];
        let selected = position == switcher.selected;
        let title_style = if selected {
            Style::default()
                .fg(Color::Cyan)
                .add_modifier(Modifier::BOLD)
        } else {
            Style::default().add_modifier(Modifier::BOLD)
        };
        let key = if position < 9 {
            format!("{}  ", position + 1)
        } else {
            "   ".to_owned()
        };
        let title_width = usize::from(areas[1].width.saturating_sub(5));
        let mut metadata = status_spans(session.status());
        metadata.push(Span::styled(" · ", Style::default().fg(Color::DarkGray)));
        metadata.push(Span::styled(
            relative_activity(session_match.latest_activity_ms),
            Style::default().fg(Color::DarkGray),
        ));
        metadata.push(Span::styled("  ", Style::default().fg(Color::DarkGray)));
        metadata.push(Span::styled(
            session.agent_id().to_owned(),
            Style::default().fg(Color::DarkGray),
        ));
        let activity = match app.session_activity(session.session_id()) {
            Some(activity) => ellipsize(&activity, title_width),
            None => "No activity yet".to_owned(),
        };
        let title = Line::from(vec![
            Span::styled(key, Style::default().fg(Color::DarkGray)),
            Span::styled(
                ellipsize(&app.session_title(session.session_id()), title_width),
                title_style,
            ),
        ]);
        if areas[1].height < 6 {
            rows.push(ListItem::new(title));
        } else {
            rows.push(ListItem::new(vec![
                title,
                Line::from(metadata),
                Line::from(Span::styled(
                    format!("   {activity}"),
                    Style::default().fg(Color::DarkGray),
                )),
            ]));
        }
    }
    let sessions = List::new(rows)
        .highlight_symbol("› ")
        .highlight_style(Style::default().add_modifier(Modifier::BOLD));
    let mut state = ListState::default();
    state.select(Some(switcher.selected.min(matches.len() - 1)));
    frame.render_stateful_widget(sessions, areas[1], &mut state);
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
                format!("× Failed: {error}"),
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
                "Press n and tell Zeta what to do.",
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
            Paragraph::new(vec![
                Line::from(Span::styled(
                    "No conversation yet",
                    Style::default().add_modifier(Modifier::BOLD),
                )),
                Line::from(Span::styled(
                    "Press i to message Zeta.",
                    Style::default().fg(Color::DarkGray),
                )),
            ]),
            area,
        );
        return;
    }

    let mut lines = Vec::new();
    let width = usize::from(area.width.saturating_sub(2)).max(1);
    let focused = app.view_state().focused_timeline_item;
    for (index, item) in items.into_iter().enumerate() {
        let selected = focused == Some(index);
        let item_width = if selected {
            width.saturating_sub(2).max(1)
        } else {
            width
        };
        for mut line in timeline_item_lines(item, item_width) {
            if selected {
                line.spans
                    .insert(0, Span::styled("▌ ", Style::default().fg(Color::Cyan)));
            }
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
    let constraints = match &app.connection {
        ConnectionStatus::Connected => [Constraint::Percentage(70), Constraint::Percentage(30)],
        ConnectionStatus::Reconnecting { .. } => {
            [Constraint::Percentage(40), Constraint::Percentage(60)]
        }
    };
    let areas = Layout::horizontal(constraints).split(area);
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

    let right = match (&app.connection, app.view_state().timeline_mode) {
        (ConnectionStatus::Connected, TimelineMode::Semantic) => Vec::new(),
        (ConnectionStatus::Connected, TimelineMode::Raw) => {
            let cursor = match app.cursor {
                Some(cursor) => cursor.0.to_string(),
                None => "none".to_owned(),
            };
            vec![
                Line::from(Span::styled(
                    "raw events",
                    Style::default().fg(Color::Yellow),
                )),
                Line::from(Span::styled(
                    format!("protocol {} · cursor {cursor}", app.protocol),
                    Style::default().fg(Color::DarkGray),
                )),
            ]
        }
        (
            ConnectionStatus::Reconnecting {
                attempt,
                retry_delay_ms,
                error,
            },
            TimelineMode::Semantic | TimelineMode::Raw,
        ) => vec![
            Line::from(Span::styled(
                format!(
                    "reconnecting · attempt {attempt} · retry in {}",
                    compact_duration(*retry_delay_ms)
                ),
                Style::default().fg(Color::Yellow),
            )),
            Line::from(Span::styled(
                ellipsize(error, usize::from(areas[1].width)),
                Style::default().fg(Color::DarkGray),
            )),
        ],
    };
    frame.render_widget(Paragraph::new(right).alignment(Alignment::Right), areas[1]);
}

fn compact_duration(duration_ms: u64) -> String {
    if duration_ms < 1_000 {
        return format!("{duration_ms}ms");
    }
    let seconds = duration_ms / 1_000;
    format!("{seconds}s")
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
    if app.help {
        push_key_hint(&mut spans, "esc", "close");
        return Line::from(spans);
    }
    if let Some(feedback) = &app.feedback {
        spans.push(Span::styled(
            format!("{} {}", feedback.glyph, feedback.text),
            Style::default().fg(feedback.color),
        ));
        return Line::from(spans);
    }
    if app.switcher.is_some() {
        push_key_hint(&mut spans, "↑/↓", "choose");
        push_key_hint(&mut spans, "enter", "attach");
        push_key_hint(&mut spans, "esc", "cancel");
        return Line::from(spans);
    }
    match (&app.view, &app.mode) {
        (View::Sessions, Mode::Browse) => {
            push_key_hint(&mut spans, "enter", "attach");
            push_key_hint(&mut spans, "n", "new");
            push_key_hint(&mut spans, "?", "help");
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
            push_key_hint(&mut spans, "i", "message");
            push_key_hint(&mut spans, "/", "switch");
            push_key_hint(&mut spans, "?", "help");
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
    MarkdownRenderer::new(width).render(&content)
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
    use ratatui::buffer::Buffer;
    use ratatui::layout::Rect;
    use ratatui::style::{Color, Modifier};

    use super::{
        App, AppAction, ClipboardError, MAX_CLIPBOARD_BYTES, TerminalCapabilities,
        apply_terminal_capabilities, draw, osc52_sequence,
    };
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
        assert!(screen.contains("Press n and tell Zeta what to do."));
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
        assert!(screen.contains("Read README.md"));
        assert!(!screen.contains("Thinking"));
        assert!(!screen.contains("Completed"));
        assert!(!screen.contains("runtime.queue_item.available"));
        assert!(!screen.contains("queue_item_id"));
        assert!(screen.contains("? help"));

        let (_, metadata_row) = text_position(&screen, "zeta.master");
        let (you_column, you_row) = text_position(&screen, "You");
        let (zeta_column, zeta_row) = text_position(&screen, "Zeta");
        let (_, tool_row) = text_position(&screen, "Read README.md");

        assert!(you_row > metadata_row + 1);
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
    fn markdown_agent_output_has_scanable_hierarchy_and_preserves_code() {
        let request = event(
            "session.message.requested",
            serde_json::json!({"message": "Summarize the implementation"}),
            "session_1",
            "run_1",
            1,
        );
        let model = event(
            "zeta.model_call.completed",
            serde_json::json!({
                "content": concat!(
                    "## Result\n\n",
                    "Use **carefully**, inspect *before changing*, and open `src/main.rs:42`.\n\n",
                    "- This list item is deliberately long enough to wrap beneath its content rather than beneath the bullet.\n\n",
                    "```rust\n",
                    "fn main() {\n",
                    "    println!(\"ready\");\n",
                    "}\n",
                    "```\n\n",
                    "Malformed **markup remains readable"
                )
            }),
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
        assert_eq!(app.handle_event(&enter), AppAction::None);
        let backend = TestBackend::new(80, 32);
        let mut terminal = Terminal::new(backend).expect("terminal should initialize");

        terminal
            .draw(|frame| draw(frame, &mut app))
            .expect("Markdown timeline should draw");
        let screen = terminal.backend().to_string();

        assert!(screen.contains("Result"));
        assert!(!screen.contains("## Result"));
        assert!(screen.contains("• This list item"));
        assert!(screen.contains("fn main() {"));
        assert!(screen.contains("    println!(\"ready\");"));
        assert!(
            screen.contains("Malformed **markup remains readable"),
            "{screen}"
        );

        let (heading_column, heading_row) = text_position(&screen, "Result");
        let (strong_column, strong_row) = text_position(&screen, "carefully");
        let (emphasis_column, emphasis_row) = text_position(&screen, "before changing");
        let (path_column, path_row) = text_position(&screen, "src/main.rs:42");
        assert!(
            terminal.backend().buffer()[(heading_column, heading_row)]
                .modifier
                .contains(Modifier::BOLD)
        );
        assert!(
            terminal.backend().buffer()[(strong_column, strong_row)]
                .modifier
                .contains(Modifier::BOLD)
        );
        assert!(
            terminal.backend().buffer()[(emphasis_column, emphasis_row)]
                .modifier
                .contains(Modifier::ITALIC)
        );
        assert_eq!(
            terminal.backend().buffer()[(path_column, path_row)].fg,
            Color::Cyan
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
            "zeta.tool_call.completed",
            serde_json::json!({"tool_call_id": "call_1", "name": "verify"}),
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
        assert!(!screen.contains("Verify"));

        let down = TerminalEvent::Key(KeyEvent::new(KeyCode::Down, KeyModifiers::NONE));
        assert_eq!(app.handle_event(&down), AppAction::None);
        assert_eq!(app.handle_event(&down), AppAction::None);
        terminal
            .draw(|frame| draw(frame, &mut app))
            .expect("screen should draw");
        let screen = terminal.backend().to_string();
        assert!(screen.contains("Verify"));

        let waiting = event(
            "runtime.status.update",
            serde_json::json!({"status": "waiting", "text": "Waiting for approval"}),
            "session_1",
            "run_2",
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
            "zeta.turn.failed",
            serde_json::json!({"reason": "Review failed"}),
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
        assert!(!screen.contains("Review failed"));

        assert_eq!(app.handle_event(&live), AppAction::None);
        terminal
            .draw(|frame| draw(frame, &mut app))
            .expect("live timeline should draw");
        let screen = terminal.backend().to_string();
        assert!(screen.contains("↓ live"));
        assert!(screen.contains("Review failed"));
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
                "zeta.turn.failed",
                serde_json::json!({"reason": "New first output failed"}),
                "session_1",
                "run_3",
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
        assert!(screen.contains("Failed: session is unavailable"));
        assert!(screen.contains("↺ Draft restored for retry"));
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
    fn fuzzy_switcher_ranks_running_matches_and_attaches_without_losing_list_selection() {
        let release_idle = event(
            "session.message.requested",
            serde_json::json!({"message": "Prepare the release notes"}),
            "session_idle",
            "run_1",
            1,
        );
        let docs = event(
            "session.message.requested",
            serde_json::json!({"message": "Review the documentation"}),
            "session_docs",
            "run_2",
            2,
        );
        let release_running = event(
            "session.message.requested",
            serde_json::json!({"message": "Verify release packaging"}),
            "session_running",
            "run_3",
            3,
        );
        let mut app = App::connected(
            "0.1".to_owned(),
            vec![
                session("session_idle", "zeta.master", "idle"),
                session("session_docs", "zeta.master", "idle"),
                session("session_running", "zeta.master", "running"),
            ],
            vec![release_idle, docs, release_running],
            Some(Cursor(3)),
        );
        let down = TerminalEvent::Key(KeyEvent::new(KeyCode::Down, KeyModifiers::NONE));
        let switcher = TerminalEvent::Key(KeyEvent::new(KeyCode::Char('/'), KeyModifiers::NONE));
        assert_eq!(app.handle_event(&down), AppAction::None);
        assert_eq!(app.selected_session_id(), Some("session_docs"));
        assert_eq!(app.handle_event(&switcher), AppAction::None);
        for character in "release".chars() {
            let input =
                TerminalEvent::Key(KeyEvent::new(KeyCode::Char(character), KeyModifiers::NONE));
            assert_eq!(app.handle_event(&input), AppAction::None);
        }

        let backend = TestBackend::new(80, 18);
        let mut terminal = Terminal::new(backend).expect("terminal should initialize");
        terminal
            .draw(|frame| draw(frame, &mut app))
            .expect("switcher should draw");
        let screen = terminal.backend().to_string();
        assert!(screen.contains("Switch sessions"));
        assert!(screen.contains("/ release"));
        assert!(!screen.contains("Review the documentation"));
        let (_, running_row) = text_position(&screen, "Verify release packaging");
        let (_, idle_row) = text_position(&screen, "Prepare the release notes");
        assert!(running_row < idle_row);

        let escape = TerminalEvent::Key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        assert_eq!(app.handle_event(&escape), AppAction::None);
        assert_eq!(app.selected_session_id(), Some("session_docs"));
        assert_eq!(app.attached_session_id(), None);

        assert_eq!(app.handle_event(&switcher), AppAction::None);
        let enter = TerminalEvent::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert_eq!(app.handle_event(&enter), AppAction::None);
        assert_eq!(app.attached_session_id(), Some("session_running"));
    }

    #[test]
    fn switcher_handles_unicode_empty_results_resize_and_numbered_attachment() {
        let unicode = event(
            "session.message.requested",
            serde_json::json!({"message": "Inspect 🦀 release behavior"}),
            "session_crab",
            "run_1",
            1,
        );
        let other = event(
            "session.message.requested",
            serde_json::json!({"message": "Check the changelog"}),
            "session_other",
            "run_2",
            2,
        );
        let mut app = App::connected(
            "0.1".to_owned(),
            vec![
                session("session_crab", "zeta.master", "idle"),
                session("session_other", "zeta.master", "idle"),
            ],
            vec![unicode, other],
            Some(Cursor(2)),
        );
        let switcher = TerminalEvent::Key(KeyEvent::new(KeyCode::Char('/'), KeyModifiers::NONE));
        assert_eq!(app.handle_event(&switcher), AppAction::None);
        let crab = TerminalEvent::Key(KeyEvent::new(KeyCode::Char('🦀'), KeyModifiers::NONE));
        assert_eq!(app.handle_event(&crab), AppAction::None);
        let backend = TestBackend::new(38, 10);
        let mut terminal = Terminal::new(backend).expect("terminal should initialize");
        terminal
            .draw(|frame| draw(frame, &mut app))
            .expect("unicode switcher should draw narrowly");
        let screen = terminal.backend().to_string();
        assert!(screen.contains("Inspect 🦀 release"), "{screen}");

        let impossible = TerminalEvent::Key(KeyEvent::new(KeyCode::Char('z'), KeyModifiers::NONE));
        for _ in 0..4 {
            assert_eq!(app.handle_event(&impossible), AppAction::None);
        }
        terminal
            .draw(|frame| draw(frame, &mut app))
            .expect("empty switcher should draw");
        assert!(
            terminal
                .backend()
                .to_string()
                .contains("No matching sessions")
        );

        let escape = TerminalEvent::Key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        assert_eq!(app.handle_event(&escape), AppAction::None);
        let second = TerminalEvent::Key(KeyEvent::new(KeyCode::Char('2'), KeyModifiers::NONE));
        assert_eq!(app.handle_event(&second), AppAction::None);
        assert_eq!(app.attached_session_id(), Some("session_other"));
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
        assert!(screen.contains("Reading README.md…"));
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
        assert!(before.contains("✓ Read README.md · 1.5s"));

        app.advance_animation();
        terminal
            .draw(|frame| draw(frame, &mut app))
            .expect("completed activity should redraw");
        let after = terminal.backend().to_string();
        assert_eq!(before, after);
    }

    #[test]
    fn progress_projection_collapses_a_turn_into_one_stable_summary() {
        let request = event(
            "session.message.requested",
            serde_json::json!({"message": "Inspect the release"}),
            "session_1",
            "run_1",
            1,
        );
        let thinking = event(
            "runtime.status.update",
            serde_json::json!({"status": "reasoning_delta"}),
            "session_1",
            "run_1",
            2,
        );
        let first_started = event(
            "zeta.tool_call.started",
            serde_json::json!({
                "tool_call_id": "call_1",
                "name": "read",
                "input": {"path": "README.md"}
            }),
            "session_1",
            "run_1",
            100,
        );
        let first_completed = event(
            "zeta.tool_call.completed",
            serde_json::json!({"tool_call_id": "call_1", "name": "read"}),
            "session_1",
            "run_1",
            500,
        );
        let second_started = event(
            "zeta.tool_call.started",
            serde_json::json!({
                "tool_call_id": "call_2",
                "name": "read",
                "input": {"path": "CHANGELOG.md"}
            }),
            "session_1",
            "run_1",
            700,
        );
        let second_completed = event(
            "zeta.tool_call.completed",
            serde_json::json!({"tool_call_id": "call_2", "name": "read"}),
            "session_1",
            "run_1",
            1_500,
        );
        let model = event(
            "zeta.model_call.completed",
            serde_json::json!({"content": "The release is ready."}),
            "session_1",
            "run_1",
            1_501,
        );
        let completed = event(
            "zeta.turn.completed",
            serde_json::json!({"content": "done"}),
            "session_1",
            "run_1",
            1_502,
        );
        let mut app = App::connected(
            "0.1".to_owned(),
            vec![session("session_1", "zeta.master", "completed")],
            vec![
                request,
                thinking,
                first_started,
                first_completed,
                second_started,
                second_completed,
                model,
                completed,
            ],
            Some(Cursor(1_502)),
        );
        let enter = TerminalEvent::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert_eq!(app.handle_event(&enter), AppAction::None);

        assert_eq!(
            app.timeline_items(),
            vec![
                super::TimelineItem::User("Inspect the release".to_owned()),
                super::TimelineItem::AgentHeading,
                super::TimelineItem::Activity {
                    glyph: "✓".to_owned(),
                    text: "Inspected 2 files · 1.4s".to_owned(),
                    color: Color::Green,
                },
                super::TimelineItem::Agent("The release is ready.".to_owned()),
            ]
        );
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
        assert!(before.contains("· Reading README.md…"));

        app.advance_animation();
        terminal
            .draw(|frame| draw(frame, &mut app))
            .expect("active activity should redraw");
        let after = terminal.backend().to_string();
        assert!(after.contains("∙ Reading README.md…"));
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

    #[test]
    fn reconnecting_chrome_is_actionable_without_disturbing_local_work() {
        let mut app = App::connected("0.1".to_owned(), Vec::new(), Vec::new(), None);
        let new = TerminalEvent::Key(KeyEvent::new(KeyCode::Char('n'), KeyModifiers::NONE));
        assert_eq!(app.handle_event(&new), AppAction::None);
        assert_eq!(
            app.handle_event(&TerminalEvent::Paste("Preserve this draft".to_owned())),
            AppAction::None
        );
        app.set_reconnecting(3, 2_000, "zeta RPC closed".to_owned());
        let backend = TestBackend::new(80, 18);
        let mut terminal = Terminal::new(backend).expect("terminal should initialize");

        terminal
            .draw(|frame| draw(frame, &mut app))
            .expect("reconnecting state should draw");
        let screen = terminal.backend().to_string();
        assert!(screen.contains("reconnecting · attempt 3 · retry in 2s"));
        assert!(screen.contains("Preserve this draft"));
        assert!(!screen.contains("● connected"));

        app.set_connected();
        terminal
            .draw(|frame| draw(frame, &mut app))
            .expect("connected state should draw");
        let screen = terminal.backend().to_string();
        assert!(!screen.contains("reconnecting"));
        assert!(!screen.contains("● connected"));
        assert!(screen.contains("Preserve this draft"));
    }

    #[test]
    fn contextual_footer_stays_quiet_and_help_reveals_the_complete_key_map() {
        let request = event(
            "session.message.requested",
            serde_json::json!({"message": "Inspect the release"}),
            "session_1",
            "run_1",
            1,
        );
        let mut app = App::connected(
            "0.1".to_owned(),
            vec![session("session_1", "zeta.master", "idle")],
            vec![request],
            Some(Cursor(1)),
        );
        let backend = TestBackend::new(80, 18);
        let mut terminal = Terminal::new(backend).expect("terminal should initialize");
        terminal
            .draw(|frame| draw(frame, &mut app))
            .expect("sessions should draw");
        let screen = terminal.backend().to_string();
        assert!(screen.contains("enter attach"));
        assert!(screen.contains("n new"));
        assert!(screen.contains("? help"));
        assert!(!screen.contains("q quit"));
        assert!(!screen.contains("↑/↓ sessions"));

        let help = TerminalEvent::Key(KeyEvent::new(KeyCode::Char('?'), KeyModifiers::NONE));
        assert_eq!(app.handle_event(&help), AppAction::None);
        terminal
            .draw(|frame| draw(frame, &mut app))
            .expect("help should draw");
        let screen = terminal.backend().to_string();
        assert!(screen.contains("Session keys"));
        assert!(screen.contains("Switch sessions"));
        assert!(screen.contains("Attach directly"));
        assert!(screen.contains("Quit"));
        assert!(screen.contains("esc close"));

        let escape = TerminalEvent::Key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        assert_eq!(app.handle_event(&escape), AppAction::None);
        assert_eq!(
            app.handle_event(&TerminalEvent::Resize(42, 10)),
            AppAction::None
        );
        terminal.backend_mut().resize(42, 10);
        terminal
            .resize(Rect::new(0, 0, 42, 10))
            .expect("terminal should resize");
        terminal
            .draw(|frame| draw(frame, &mut app))
            .expect("narrow sessions should draw");
        assert!(terminal.backend().to_string().contains("? help"));
    }

    #[test]
    fn submission_confirmation_replaces_controls_briefly_then_clears() {
        let mut app = App::connected("0.1".to_owned(), Vec::new(), Vec::new(), None);
        app.submission_started("message-1".to_owned(), "Prepare release".to_owned());
        app.submission_queued("message-1", "evt_1", "session_1");
        let backend = TestBackend::new(80, 18);
        let mut terminal = Terminal::new(backend).expect("terminal should initialize");

        terminal
            .draw(|frame| draw(frame, &mut app))
            .expect("confirmation should draw");
        assert!(terminal.backend().to_string().contains("✓ Sent"));
        for _ in 0..5 {
            app.advance_animation();
        }
        terminal
            .draw(|frame| draw(frame, &mut app))
            .expect("controls should return");
        let screen = terminal.backend().to_string();
        assert!(!screen.contains("✓ Sent"));
        assert!(screen.contains("i message"));
    }

    #[test]
    fn attached_empty_state_explains_how_to_begin() {
        let mut app = App::connected(
            "0.1".to_owned(),
            vec![session("session_1", "zeta.master", "idle")],
            Vec::new(),
            None,
        );
        let enter = TerminalEvent::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert_eq!(app.handle_event(&enter), AppAction::None);
        let backend = TestBackend::new(80, 18);
        let mut terminal = Terminal::new(backend).expect("terminal should initialize");

        terminal
            .draw(|frame| draw(frame, &mut app))
            .expect("empty conversation should draw");
        let screen = terminal.backend().to_string();
        assert!(screen.contains("No conversation yet"));
        assert!(screen.contains("Press i to message Zeta."));
    }

    #[test]
    fn transcript_focus_copies_semantic_items_without_losing_wrapped_content() {
        let request = event(
            "session.message.requested",
            serde_json::json!({"message": "Inspect every relevant file before answering"}),
            "session_1",
            "run_1",
            1,
        );
        let response = event(
            "zeta.model_call.completed",
            serde_json::json!({"content": "The implementation is ready for review."}),
            "session_1",
            "run_1",
            2,
        );
        let mut app = App::connected(
            "0.1".to_owned(),
            vec![session("session_1", "zeta.master", "idle")],
            vec![request, response],
            Some(Cursor(2)),
        );
        let enter = TerminalEvent::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert_eq!(app.handle_event(&enter), AppAction::None);

        let copy = TerminalEvent::Key(KeyEvent::new(KeyCode::Char('y'), KeyModifiers::NONE));
        assert_eq!(
            app.handle_event(&copy),
            AppAction::Copy("The implementation is ready for review.".to_owned())
        );
        let previous = TerminalEvent::Key(KeyEvent::new(KeyCode::BackTab, KeyModifiers::SHIFT));
        assert_eq!(app.handle_event(&previous), AppAction::None);
        assert_eq!(
            app.handle_event(&copy),
            AppAction::Copy("Inspect every relevant file before answering".to_owned())
        );

        let backend = TestBackend::new(36, 22);
        let mut terminal = Terminal::new(backend).expect("terminal should initialize");
        terminal
            .draw(|frame| draw(frame, &mut app))
            .expect("focused transcript should draw");
        let screen = terminal.backend().to_string();
        assert!(screen.contains('▌'));
        assert!(screen.contains("Inspect every relevant"), "{screen}");
        assert!(screen.contains("before answering"), "{screen}");
    }

    #[test]
    fn osc52_clipboard_is_bounded_and_keeps_hostile_controls_encoded() {
        assert_eq!(
            osc52_sequence("hello", false),
            Err(ClipboardError::Unsupported)
        );
        assert_eq!(
            osc52_sequence(&"a".repeat(MAX_CLIPBOARD_BYTES + 1), true),
            Err(ClipboardError::TooLarge)
        );

        let sequence = osc52_sequence("safe\u{1b}]52;c;hostile\u{7}", true)
            .expect("supported, bounded text should encode");
        assert!(sequence.starts_with("\u{1b}]52;c;"));
        assert!(sequence.ends_with('\u{7}'));
        assert_eq!(sequence.matches("\u{1b}]52").count(), 1);
        assert_eq!(sequence.matches('\u{7}').count(), 1);
    }

    #[test]
    fn no_color_rendering_preserves_text_and_modifiers() {
        let mut app = App::connected(
            "0.1".to_owned(),
            vec![session("session_1", "zeta.master", "running")],
            Vec::new(),
            None,
        );
        app.set_terminal_capabilities(TerminalCapabilities {
            color: false,
            osc8: false,
            osc52: false,
        });
        let backend = TestBackend::new(80, 18);
        let mut terminal = Terminal::new(backend).expect("terminal should initialize");
        terminal
            .draw(|frame| draw(frame, &mut app))
            .expect("no-color screen should draw");

        assert!(terminal.backend().to_string().contains("zeta"));
        for cell in terminal.backend().buffer().content() {
            assert_eq!(cell.fg, Color::Reset);
            assert_eq!(cell.bg, Color::Reset);
        }
    }

    #[test]
    fn osc8_links_only_existing_local_files_and_preserves_plain_labels() {
        let area = Rect::new(0, 0, 40, 1);
        let mut unsupported = Buffer::with_lines(["Open ./Cargo.toml and missing/file.rs"]);
        let plain = unsupported.clone();
        apply_terminal_capabilities(
            &mut unsupported,
            TerminalCapabilities {
                color: true,
                osc8: false,
                osc52: false,
            },
        );
        assert_eq!(unsupported, plain);

        let mut supported = Buffer::with_lines(["Open ./Cargo.toml and missing/file.rs"]);
        supported.resize(area);
        apply_terminal_capabilities(
            &mut supported,
            TerminalCapabilities {
                color: true,
                osc8: true,
                osc52: false,
            },
        );
        let symbols = supported
            .content()
            .iter()
            .map(|cell| cell.symbol())
            .collect::<String>();
        assert!(symbols.contains("\u{1b}]8;;file://"));
        assert!(symbols.contains("Cargo"));
        assert!(symbols.contains("missing/file"));
        assert_eq!(symbols.matches("\u{1b}]8;;file://").count(), 6);
    }

    #[test]
    fn terminal_controls_cover_quit_suspend_resize_bursts_and_large_paste() {
        let mut app = App::connected("0.1".to_owned(), Vec::new(), Vec::new(), None);
        let control_c =
            TerminalEvent::Key(KeyEvent::new(KeyCode::Char('c'), KeyModifiers::CONTROL));
        let control_z =
            TerminalEvent::Key(KeyEvent::new(KeyCode::Char('z'), KeyModifiers::CONTROL));
        assert_eq!(app.handle_event(&control_c), AppAction::Quit);
        assert_eq!(app.handle_event(&control_z), AppAction::Suspend);

        let new = TerminalEvent::Key(KeyEvent::new(KeyCode::Char('n'), KeyModifiers::NONE));
        assert_eq!(app.handle_event(&new), AppAction::None);
        let pasted = "release🙂\n".repeat(20_000);
        assert_eq!(
            app.handle_event(&TerminalEvent::Paste(pasted.clone())),
            AppAction::None
        );
        assert_eq!(app.new_session_view.draft.text, pasted);

        for width in 20..120 {
            assert_eq!(
                app.handle_event(&TerminalEvent::Resize(width, 24)),
                AppAction::None
            );
        }
        let backend = TestBackend::new(42, 12);
        let mut terminal = Terminal::new(backend).expect("terminal should initialize");
        terminal
            .draw(|frame| draw(frame, &mut app))
            .expect("large paste should remain renderable after resize bursts");
        assert!(terminal.backend().to_string().contains("line 20001/20001"));
    }

    #[test]
    fn detaching_from_a_new_session_keeps_it_selected_for_reattachment() {
        let mut app = App::connected(
            "0.1".to_owned(),
            vec![session("session_old", "zeta.master", "idle")],
            Vec::new(),
            None,
        );
        app.submission_started("message-1".to_owned(), "Start something new".to_owned());
        app.submission_queued("message-1", "evt_new", "session_new");
        app.replace_sessions(vec![
            session("session_old", "zeta.master", "idle"),
            session("session_new", "zeta.master", "running"),
        ]);

        let escape = TerminalEvent::Key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        assert_eq!(app.handle_event(&escape), AppAction::None);
        assert_eq!(app.selected_session_id(), Some("session_new"));
        let enter = TerminalEvent::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert_eq!(app.handle_event(&enter), AppAction::None);
        assert_eq!(app.attached_session_id(), Some("session_new"));
    }
}
