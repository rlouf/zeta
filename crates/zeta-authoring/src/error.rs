//! Structured authored-declaration failures.

use std::fmt;
use std::path::{Path, PathBuf};

/// Classifies why an authored agent cannot be parsed.
///
/// # Examples
///
/// ```
/// use std::path::Path;
///
/// let error = zeta_authoring::parse_agent(Path::new("worker.md"), b"no frontmatter")
///     .unwrap_err();
/// assert_eq!(
///     error.kind(),
///     zeta_authoring::SpecErrorKind::MissingFrontmatterDelimiter
/// );
/// ```
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SpecErrorKind {
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
    /// The logical filename does not produce a valid agent slug.
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

/// Reports one authored-agent parsing failure.
///
/// The kind and field are stable enough for callers to present structured
/// diagnostics. The message gives the source-specific detail.
///
/// # Examples
///
/// ```
/// use std::path::Path;
///
/// let error = zeta_authoring::parse_agent(
///     Path::new("worker.md"),
///     b"---\ndescription: Missing a name.\n---\n",
/// )
/// .unwrap_err();
/// assert_eq!(error.field(), Some("name"));
/// assert_eq!(error.path(), Path::new("worker.md"));
/// ```
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SpecError {
    kind: SpecErrorKind,
    field: Option<String>,
    path: PathBuf,
    detail: String,
}

impl SpecError {
    pub(crate) fn new(
        kind: SpecErrorKind,
        field: Option<&str>,
        path: &Path,
        detail: impl Into<String>,
    ) -> Self {
        SpecError {
            kind,
            field: field.map(str::to_owned),
            path: path.to_path_buf(),
            detail: detail.into(),
        }
    }

    /// Returns the stable failure class.
    ///
    /// # Examples
    ///
    /// ```
    /// use std::path::Path;
    ///
    /// let error = zeta_authoring::parse_agent(Path::new("worker.md"), b"bad")
    ///     .unwrap_err();
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
    /// use std::path::Path;
    ///
    /// let error = zeta_authoring::parse_agent(
    ///     Path::new("worker.md"),
    ///     b"---\nname: Worker\ndescription: Works.\nenabled: maybe\n---\n",
    /// )
    /// .unwrap_err();
    /// assert_eq!(error.field(), Some("enabled"));
    /// ```
    pub fn field(&self) -> Option<&str> {
        self.field.as_deref()
    }

    /// Returns the caller-supplied logical source path.
    ///
    /// # Examples
    ///
    /// ```
    /// use std::path::Path;
    ///
    /// let error = zeta_authoring::parse_agent(Path::new("worker.md"), b"bad")
    ///     .unwrap_err();
    /// assert_eq!(error.path(), Path::new("worker.md"));
    /// ```
    pub fn path(&self) -> &Path {
        &self.path
    }
}

impl fmt::Display for SpecError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "{} in {}: {}",
            self.kind.reason(),
            self.path.display(),
            self.detail
        )
    }
}

impl std::error::Error for SpecError {}
