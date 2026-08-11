//! Structured authored-declaration loading and parsing failures.

use std::fmt;
use std::path::{Path, PathBuf};

/// Classifies why an authored agent cannot be loaded or parsed.
///
/// # Examples
///
/// ```
/// let error = zeta_authoring::parse_agent("worker", b"no frontmatter").unwrap_err();
/// assert_eq!(
///     error.kind(),
///     zeta_authoring::SpecErrorKind::MissingFrontmatterDelimiter
/// );
/// ```
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SpecErrorKind {
    /// The source path cannot be read.
    Io,
    /// The source bytes are not valid UTF-8.
    InvalidUtf8,
    /// The first line is not a frontmatter delimiter.
    MissingFrontmatterDelimiter,
    /// The source has no closing frontmatter delimiter.
    MissingClosingFrontmatterDelimiter,
    /// The frontmatter is not valid YAML.
    InvalidYaml,
    /// The frontmatter root is not an object.
    ExpectedFrontmatterObject,
    /// The supplied slug is not a valid agent identifier.
    InvalidSlug,
    /// A required declaration field is absent or empty.
    MissingRequiredField,
    /// A declaration field has the wrong shape or value.
    InvalidField,
}

impl SpecErrorKind {
    /// Returns a stable machine-readable reason.
    ///
    /// # Examples
    ///
    /// ```
    /// assert_eq!(
    ///     zeta_authoring::SpecErrorKind::InvalidSlug.reason(),
    ///     "invalid_slug"
    /// );
    /// ```
    pub fn reason(self) -> &'static str {
        match self {
            SpecErrorKind::Io => "io_error",
            SpecErrorKind::InvalidUtf8 => "invalid_utf8",
            SpecErrorKind::MissingFrontmatterDelimiter => "missing_frontmatter_delimiter",
            SpecErrorKind::MissingClosingFrontmatterDelimiter => {
                "missing_closing_frontmatter_delimiter"
            }
            SpecErrorKind::InvalidYaml => "invalid_yaml",
            SpecErrorKind::ExpectedFrontmatterObject => "expected_frontmatter_object",
            SpecErrorKind::InvalidSlug => "invalid_slug",
            SpecErrorKind::MissingRequiredField => "missing_required_field",
            SpecErrorKind::InvalidField => "invalid_field",
        }
    }
}

/// Reports one authored-agent loading or parsing failure.
///
/// The kind and field are stable enough for callers to present structured
/// diagnostics. The message gives the source-specific detail.
///
/// # Examples
///
/// ```
/// let error = zeta_authoring::parse_agent(
///     "worker",
///     b"---\ndescription: Missing a name.\n---\n",
/// )
/// .unwrap_err();
/// assert_eq!(error.field(), Some("name"));
/// assert_eq!(error.path(), None);
/// ```
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SpecError {
    kind: SpecErrorKind,
    field: Option<String>,
    path: Option<PathBuf>,
    detail: String,
}

impl SpecError {
    pub(crate) fn new(kind: SpecErrorKind, field: Option<&str>, detail: impl Into<String>) -> Self {
        SpecError {
            kind,
            field: field.map(str::to_owned),
            path: None,
            detail: detail.into(),
        }
    }

    pub(crate) fn with_path(mut self, path: &Path) -> Self {
        self.path = Some(path.to_path_buf());
        self
    }

    /// Returns the stable failure class.
    ///
    /// # Examples
    ///
    /// ```
    /// let error = zeta_authoring::parse_agent("worker", b"bad").unwrap_err();
    /// assert_eq!(
    ///     error.kind(),
    ///     zeta_authoring::SpecErrorKind::MissingFrontmatterDelimiter
    /// );
    /// ```
    pub fn kind(&self) -> SpecErrorKind {
        self.kind
    }

    /// Returns the declaration field responsible for the failure.
    ///
    /// # Examples
    ///
    /// ```
    /// let error = zeta_authoring::parse_agent(
    ///     "worker",
    ///     b"---\nname: Worker\ndescription: Works.\nenabled: maybe\n---\n",
    /// )
    /// .unwrap_err();
    /// assert_eq!(error.field(), Some("enabled"));
    /// ```
    pub fn field(&self) -> Option<&str> {
        self.field.as_deref()
    }

    /// Returns the filesystem path attached by [`load_agent`], if present.
    ///
    /// # Examples
    ///
    /// ```
    /// let error = zeta_authoring::parse_agent("worker", b"bad").unwrap_err();
    /// assert_eq!(error.path(), None);
    /// ```
    ///
    /// [`load_agent`]: crate::load_agent
    pub fn path(&self) -> Option<&Path> {
        self.path.as_deref()
    }
}

impl fmt::Display for SpecError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        let Some(path) = &self.path else {
            return write!(formatter, "{}: {}", self.kind.reason(), self.detail);
        };
        write!(
            formatter,
            "{} in {}: {}",
            self.kind.reason(),
            path.display(),
            self.detail
        )
    }
}

impl std::error::Error for SpecError {}
