//! Structured authored-declaration loading and parsing failures.

use std::fmt;
use std::path::{Path, PathBuf};

/// Classifies why an authored agent cannot be loaded or parsed.
///
/// # Examples
///
/// ```
/// let error = zeta_manifest::parse_agent("worker", b"no frontmatter").unwrap_err();
/// assert_eq!(
///     error.kind(),
///     zeta_manifest::SpecErrorKind::MissingFrontmatterDelimiter
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
    ///     zeta_manifest::SpecErrorKind::InvalidSlug.reason(),
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
/// let error = zeta_manifest::parse_agent(
///     "worker",
///     b"---\ndescription: Missing a name.\n---\n",
/// )
/// .unwrap_err();
/// assert_eq!(error.field(), Some("name"));
/// assert_eq!(error.path(), None);
/// ```
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AgentSpecError {
    kind: SpecErrorKind,
    field: Option<String>,
    path: Option<PathBuf>,
    detail: String,
}

impl AgentSpecError {
    pub(crate) fn new(kind: SpecErrorKind, field: Option<&str>, detail: impl Into<String>) -> Self {
        AgentSpecError {
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
    /// let error = zeta_manifest::parse_agent("worker", b"bad").unwrap_err();
    /// assert_eq!(
    ///     error.kind(),
    ///     zeta_manifest::SpecErrorKind::MissingFrontmatterDelimiter
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
    /// let error = zeta_manifest::parse_agent(
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
    /// let error = zeta_manifest::parse_agent("worker", b"bad").unwrap_err();
    /// assert_eq!(error.path(), None);
    /// ```
    ///
    /// [`load_agent`]: crate::load_agent
    pub fn path(&self) -> Option<&Path> {
        self.path.as_deref()
    }
}

impl fmt::Display for AgentSpecError {
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

impl std::error::Error for AgentSpecError {}

/// Classifies why authored declarations cannot form a valid project.
///
/// # Examples
///
/// ```
/// assert_eq!(
///     zeta_manifest::ManifestErrorKind::InvalidSchema.reason(),
///     "invalid_schema"
/// );
/// ```
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ManifestErrorKind {
    /// A declaration carries a malformed Draft 2020-12 JSON Schema.
    InvalidSchema,
    /// Two declarations assign different values to the same identity.
    ConflictingDeclaration,
    /// A declaration identity occurs more than once where merging is forbidden.
    DuplicateDeclaration,
    /// An agent references an event outside the supplied vocabulary.
    UnknownEvent,
    /// An authored prompt is not valid template syntax.
    InvalidPromptSyntax,
    /// An authored prompt references a root other than `event`.
    UnknownPromptRoot,
    /// A valid authored prompt cannot render its supplied event.
    PromptRender,
    /// A supplied skill declaration is malformed.
    InvalidSkill,
    /// A supplied connector declaration is malformed.
    InvalidConnector,
    /// A supplied capability declaration is malformed.
    InvalidCapability,
    /// A supplied executor-provider declaration is malformed.
    InvalidExecutorProvider,
    /// A supplied model-selection declaration is malformed.
    InvalidModel,
    /// A supplied typed agent declaration violates the parser contract.
    InvalidAgent,
    /// An agent references a tool outside the supplied vocabulary.
    UnknownTool,
    /// An agent lists a runtime-reserved tool.
    ReservedTool,
    /// An agent references a skill outside the supplied vocabulary.
    UnknownSkill,
    /// An agent selects an executor provider outside the supplied vocabulary.
    UnknownExecutorProvider,
    /// An agent carries a frontmatter extension without a declared owner.
    UnknownExtension,
    /// A connector binding does not satisfy its declaration or schema.
    InvalidBinding,
    /// A project or execution manifest has an invalid shape or version.
    InvalidManifest,
    /// A content-addressed declaration fails identity verification.
    InvalidIdentity,
    /// An operation names an agent outside the compiled project.
    UnknownAgent,
}

impl ManifestErrorKind {
    /// Returns a stable machine-readable reason.
    ///
    /// # Examples
    ///
    /// ```
    /// assert_eq!(
    ///     zeta_manifest::ManifestErrorKind::UnknownEvent.reason(),
    ///     "unknown_event"
    /// );
    /// ```
    pub fn reason(self) -> &'static str {
        match self {
            ManifestErrorKind::InvalidSchema => "invalid_schema",
            ManifestErrorKind::ConflictingDeclaration => "conflicting_declaration",
            ManifestErrorKind::DuplicateDeclaration => "duplicate_declaration",
            ManifestErrorKind::UnknownEvent => "unknown_event",
            ManifestErrorKind::InvalidPromptSyntax => "invalid_prompt_syntax",
            ManifestErrorKind::UnknownPromptRoot => "unknown_prompt_root",
            ManifestErrorKind::PromptRender => "prompt_render",
            ManifestErrorKind::InvalidSkill => "invalid_skill",
            ManifestErrorKind::InvalidConnector => "invalid_connector",
            ManifestErrorKind::InvalidCapability => "invalid_capability",
            ManifestErrorKind::InvalidExecutorProvider => "invalid_executor_provider",
            ManifestErrorKind::InvalidModel => "invalid_model",
            ManifestErrorKind::InvalidAgent => "invalid_agent",
            ManifestErrorKind::UnknownTool => "unknown_tool",
            ManifestErrorKind::ReservedTool => "reserved_tool",
            ManifestErrorKind::UnknownSkill => "unknown_skill",
            ManifestErrorKind::UnknownExecutorProvider => "unknown_executor_provider",
            ManifestErrorKind::UnknownExtension => "unknown_extension",
            ManifestErrorKind::InvalidBinding => "invalid_binding",
            ManifestErrorKind::InvalidManifest => "invalid_manifest",
            ManifestErrorKind::InvalidIdentity => "invalid_identity",
            ManifestErrorKind::UnknownAgent => "unknown_agent",
        }
    }
}

/// Reports one declaration validation or manifest compilation failure.
///
/// The kind is stable for machine handling. Subject and field context identify
/// the declaration without introducing filesystem provenance.
///
/// # Examples
///
/// ```
/// let error = zeta_manifest::derive_returns_schema(
///     &zeta_manifest::parse_agent(
///         "worker",
///         b"---\nname: Worker\ndescription: Works.\nreturns: [missing]\n---\n",
///     )?,
///     &zeta_manifest::EventRegistry::new(),
/// )
/// .unwrap_err();
/// assert_eq!(error.subject(), Some("missing"));
/// # Ok::<(), zeta_manifest::AgentSpecError>(())
/// ```
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ManifestError {
    kind: ManifestErrorKind,
    subject: Option<String>,
    field: Option<String>,
    detail: String,
}

impl ManifestError {
    pub(crate) fn new(
        kind: ManifestErrorKind,
        subject: Option<&str>,
        field: Option<&str>,
        detail: impl Into<String>,
    ) -> Self {
        ManifestError {
            kind,
            subject: subject.map(str::to_owned),
            field: field.map(str::to_owned),
            detail: detail.into(),
        }
    }

    /// Returns the stable failure class.
    ///
    /// # Examples
    ///
    /// ```
    /// let mut events = zeta_manifest::EventRegistry::new();
    /// let error = events
    ///     .register(
    ///         "bad",
    ///         Some(serde_json::from_value(serde_json::json!({"type": "bad"})).unwrap()),
    ///     )
    ///     .unwrap_err();
    /// assert_eq!(error.kind(), zeta_manifest::ManifestErrorKind::InvalidSchema);
    /// ```
    pub fn kind(&self) -> ManifestErrorKind {
        self.kind
    }

    /// Returns the declaration identity responsible for the failure.
    ///
    /// # Examples
    ///
    /// ```
    /// let spec = zeta_manifest::parse_agent(
    ///     "worker",
    ///     b"---\nname: Worker\ndescription: Works.\nreturns: [missing]\n---\n",
    /// )?;
    /// let error = zeta_manifest::derive_returns_schema(
    ///     &spec,
    ///     &zeta_manifest::EventRegistry::new(),
    /// )
    /// .unwrap_err();
    /// assert_eq!(error.subject(), Some("missing"));
    /// # Ok::<(), zeta_manifest::AgentSpecError>(())
    /// ```
    pub fn subject(&self) -> Option<&str> {
        self.subject.as_deref()
    }

    /// Returns the declaration field responsible for the failure.
    ///
    /// # Examples
    ///
    /// ```
    /// let spec = zeta_manifest::parse_agent(
    ///     "worker",
    ///     b"---\nname: Worker\ndescription: Works.\nreturns: [missing]\n---\n",
    /// )?;
    /// let error = zeta_manifest::derive_returns_schema(
    ///     &spec,
    ///     &zeta_manifest::EventRegistry::new(),
    /// )
    /// .unwrap_err();
    /// assert_eq!(error.field(), Some("returns"));
    /// # Ok::<(), zeta_manifest::AgentSpecError>(())
    /// ```
    pub fn field(&self) -> Option<&str> {
        self.field.as_deref()
    }

    /// Returns the human-readable failure detail.
    ///
    /// # Examples
    ///
    /// ```
    /// let spec = zeta_manifest::parse_agent(
    ///     "worker",
    ///     b"---\nname: Worker\ndescription: Works.\nreturns: [missing]\n---\n",
    /// )?;
    /// let error = zeta_manifest::derive_returns_schema(
    ///     &spec,
    ///     &zeta_manifest::EventRegistry::new(),
    /// )
    /// .unwrap_err();
    /// assert!(error.detail().contains("unknown event"));
    /// # Ok::<(), zeta_manifest::AgentSpecError>(())
    /// ```
    pub fn detail(&self) -> &str {
        &self.detail
    }
}

impl fmt::Display for ManifestError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match (&self.subject, &self.field) {
            (Some(subject), Some(field)) => write!(
                formatter,
                "{} for {subject:?} in {field}: {}",
                self.kind.reason(),
                self.detail
            ),
            (Some(subject), None) => write!(
                formatter,
                "{} for {subject:?}: {}",
                self.kind.reason(),
                self.detail
            ),
            (None, Some(field)) => write!(
                formatter,
                "{} in {field}: {}",
                self.kind.reason(),
                self.detail
            ),
            (None, None) => write!(formatter, "{}: {}", self.kind.reason(), self.detail),
        }
    }
}

impl std::error::Error for ManifestError {}
