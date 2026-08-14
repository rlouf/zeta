//! Builds verified source bundles for trusted executor drivers.

use std::collections::BTreeMap;
use std::fs;
use std::path::{Component, Path, PathBuf};

use base64::Engine as _;
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use zeta_manifest::AgentSpec;
use zeta_substrate::{canonical_json, hash_bytes};

use crate::{ExecutorProfile, PythonProviderCatalog};

const MAX_BUNDLE_BYTES: usize = 2 * 1024 * 1024;

/// Contains the verified sources and definitions for one executor open request.
#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
pub struct ExecutorBundle {
    workspace: WorkspaceBundle,
    tools: ToolBundle,
}

/// Describes a source tree that an executor driver must transfer unchanged.
#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
pub struct WorkspaceBundle {
    /// Identifies the exact bundle content.
    pub id: String,
    /// Lists the transferred source files in stable path order.
    pub files: Vec<BundleFile>,
}

/// Describes the portable tool code and allowed capability declarations.
#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
pub struct ToolBundle {
    /// Identifies the exact tool source and declaration content.
    pub id: String,
    /// Lists the Python source files that implement the approved tools.
    pub files: Vec<BundleFile>,
    /// Lists the exact capability declarations available in the environment.
    pub capabilities: Vec<ExecutorCapability>,
}

/// Carries one immutable source file for a provider driver.
#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
pub struct BundleFile {
    /// Names the file relative to the project root.
    pub path: String,
    /// Identifies the exact file bytes.
    pub content_address: String,
    /// Encodes the exact file bytes for transfer to the driver.
    pub content_base64: String,
}

/// Describes one capability that the remote tool runtime can dispatch.
#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
pub struct ExecutorCapability {
    /// Carries the canonical capability identity.
    pub id: String,
    /// Explains the capability to the remote tool runtime.
    pub description: String,
    /// Validates the canonical JSON input.
    pub input_schema: Map<String, Value>,
    /// Carries the optional result schema.
    pub output_schema: Option<Map<String, Value>>,
    /// Names the approved Python source file for this capability.
    pub source_path: String,
}

impl ExecutorBundle {
    /// Builds exact workspace and tool bundles for one remote agent.
    pub fn build_for_agent(
        project_root: &Path,
        workspace: WorkspaceBundle,
        providers: &PythonProviderCatalog,
        agent: &AgentSpec,
    ) -> Result<Self, String> {
        if agent.tools_inherit {
            return Err(format!(
                "remote agent {:?} must declare each portable tool explicitly",
                agent.slug
            ));
        }
        let mut source_files = BTreeMap::new();
        let mut declarations = Vec::with_capacity(agent.tools.len());
        for id in &agent.tools {
            let provider = providers.tools().get(id).ok_or_else(|| {
                format!(
                    "remote agent {:?} has no portable implementation for capability {id:?}",
                    agent.slug
                )
            })?;
            let path = provider.source.path.as_deref().ok_or_else(|| {
                format!("remote capability {id:?} must use a project-local Python source file")
            })?;
            let source_path = project_relative_path(project_root, Path::new(path))?;
            let descriptor = read_capability_descriptor(project_root, id)?;
            let description = provider
                .description
                .clone()
                .filter(|description| !description.trim().is_empty())
                .ok_or_else(|| format!("remote capability {id:?} has no description"))?;
            let input_schema = provider
                .input_schema
                .clone()
                .ok_or_else(|| format!("remote capability {id:?} has no input schema"))?;
            if descriptor.description != description
                || descriptor.input_schema != input_schema
                || descriptor.output_schema != provider.output_schema
            {
                return Err(format!(
                    "capability descriptor {id:?} does not match its Python provider declaration"
                ));
            }
            let source = workspace
                .files
                .iter()
                .find(|file| file.path == source_path)
                .cloned()
                .ok_or_else(|| {
                    format!(
                        "workspace bundle does not include remote capability source {source_path:?}"
                    )
                })?;
            source_files.entry(source_path.clone()).or_insert(source);
            declarations.push(ExecutorCapability {
                id: id.clone(),
                description: descriptor.description,
                input_schema: descriptor.input_schema,
                output_schema: descriptor.output_schema,
                source_path,
            });
        }
        declarations.sort_by(|left, right| left.id.cmp(&right.id));
        let tools = ToolBundle::new(source_files.into_values().collect(), declarations)?;
        Ok(Self { workspace, tools })
    }

    /// Returns the verified workspace payload for one driver open request.
    pub fn workspace(&self) -> &WorkspaceBundle {
        &self.workspace
    }

    /// Returns the verified tool payload for one driver open request.
    pub fn tools(&self) -> &ToolBundle {
        &self.tools
    }

    /// Reports whether the bundle permits one capability identifier.
    pub fn permits(&self, id: &str) -> bool {
        self.tools
            .capabilities
            .iter()
            .any(|capability| capability.id == id)
    }

    /// Verifies the stored workspace, tool sources, and bundle identities.
    pub fn verify(&self) -> Result<(), String> {
        self.workspace.verify()?;
        self.tools.verify()?;
        for file in &self.tools.files {
            let Some(workspace_file) = self
                .workspace
                .files
                .iter()
                .find(|workspace_file| workspace_file.path == file.path)
            else {
                return Err(format!(
                    "tool bundle source {:?} is absent from the workspace bundle",
                    file.path
                ));
            };
            if workspace_file != file {
                return Err(format!(
                    "tool bundle source {:?} differs from the workspace bundle",
                    file.path
                ));
            }
        }
        for capability in &self.tools.capabilities {
            if !self
                .tools
                .files
                .iter()
                .any(|file| file.path == capability.source_path)
            {
                return Err(format!(
                    "capability {:?} has no source in the tool bundle",
                    capability.id
                ));
            }
        }
        Ok(())
    }
}

impl WorkspaceBundle {
    /// Builds one immutable workspace bundle from the profile include list.
    pub fn build(project_root: &Path, profile: &ExecutorProfile) -> Result<Self, String> {
        let includes = workspace_includes(profile)?;
        let files = collect_workspace_files(project_root, &includes)?;
        Self::new(files)
    }

    /// Verifies each file address and the bundle identity.
    pub fn verify(&self) -> Result<(), String> {
        let mut prior = None;
        for file in &self.files {
            validate_bundle_path(&file.path)?;
            if let Some(previous) = prior {
                if previous >= file.path.as_str() {
                    return Err("workspace bundle files are not in stable path order".to_owned());
                }
            }
            prior = Some(file.path.as_str());
            let bytes = base64::engine::general_purpose::STANDARD
                .decode(&file.content_base64)
                .map_err(|_error| {
                    format!("workspace file {:?} has invalid base64 data", file.path)
                })?;
            if file.content_address != hash_bytes(&bytes).to_string() {
                return Err(format!(
                    "workspace file {:?} has an invalid content address",
                    file.path
                ));
            }
        }
        bundle_size(&self.files)?;
        let expected = bundle_id("workspace", &self.files)?;
        if self.id != expected {
            return Err("workspace bundle has an invalid identity".to_owned());
        }
        Ok(())
    }

    fn new(files: Vec<BundleFile>) -> Result<Self, String> {
        let id = bundle_id("workspace", &files)?;
        Ok(Self { id, files })
    }
}

impl ToolBundle {
    fn new(files: Vec<BundleFile>, capabilities: Vec<ExecutorCapability>) -> Result<Self, String> {
        bundle_size(&files)?;
        let value = serde_json::json!({
            "files": files,
            "capabilities": capabilities,
        });
        let bytes = canonical_json(&value)
            .map_err(|error| format!("cannot encode the tool bundle: {error}"))?;
        Ok(Self {
            id: format!("tools:{}", hash_bytes(&bytes)),
            files,
            capabilities,
        })
    }

    fn verify(&self) -> Result<(), String> {
        let mut prior = None;
        let mut capability_ids = BTreeMap::new();
        for file in &self.files {
            validate_bundle_path(&file.path)?;
            if let Some(previous) = prior {
                if previous >= file.path.as_str() {
                    return Err("tool bundle files are not in stable path order".to_owned());
                }
            }
            prior = Some(file.path.as_str());
            let bytes = base64::engine::general_purpose::STANDARD
                .decode(&file.content_base64)
                .map_err(|_error| format!("tool file {:?} has invalid base64 data", file.path))?;
            if file.content_address != hash_bytes(&bytes).to_string() {
                return Err(format!(
                    "tool file {:?} has an invalid content address",
                    file.path
                ));
            }
        }
        bundle_size(&self.files)?;
        let mut prior_capability = None;
        for capability in &self.capabilities {
            if capability.id.is_empty() {
                return Err("tool bundle has an empty capability identifier".to_owned());
            }
            if capability.description.trim().is_empty() {
                return Err(format!(
                    "tool bundle capability {:?} has no description",
                    capability.id
                ));
            }
            validate_bundle_path(&capability.source_path)?;
            if capability_ids.insert(capability.id.as_str(), ()).is_some() {
                return Err(format!(
                    "tool bundle has a duplicate capability identifier {:?}",
                    capability.id
                ));
            }
            if let Some(previous) = prior_capability {
                if previous >= capability.id.as_str() {
                    return Err("tool bundle capabilities are not in stable order".to_owned());
                }
            }
            prior_capability = Some(capability.id.as_str());
        }
        let value = serde_json::json!({
            "files": self.files,
            "capabilities": self.capabilities,
        });
        let bytes = canonical_json(&value)
            .map_err(|error| format!("cannot encode the tool bundle: {error}"))?;
        if self.id != format!("tools:{}", hash_bytes(&bytes)) {
            return Err("tool bundle has an invalid identity".to_owned());
        }
        Ok(())
    }
}

fn bundle_id(kind: &str, files: &[BundleFile]) -> Result<String, String> {
    let value = serde_json::json!({"files": files});
    let bytes = canonical_json(&value)
        .map_err(|error| format!("cannot encode the {kind} bundle: {error}"))?;
    Ok(format!("{kind}:{}", hash_bytes(&bytes)))
}

fn workspace_includes(profile: &ExecutorProfile) -> Result<Vec<IncludePath>, String> {
    let workspace = profile
        .policy()
        .get("workspace")
        .and_then(Value::as_object)
        .ok_or_else(|| {
            format!(
                "executor profile {:?} must declare workspace.include",
                profile.provider()
            )
        })?;
    let values = workspace
        .get("include")
        .and_then(Value::as_array)
        .ok_or_else(|| {
            format!(
                "executor profile {:?} must declare workspace.include as an array",
                profile.provider()
            )
        })?;
    if values.is_empty() {
        return Err(format!(
            "executor profile {:?} must include at least one workspace path",
            profile.provider()
        ));
    }
    values
        .iter()
        .map(|value| {
            let pattern = value
                .as_str()
                .filter(|pattern| !pattern.is_empty())
                .ok_or_else(|| "workspace include paths must be non-empty strings".to_owned())?;
            IncludePath::parse(pattern)
        })
        .collect()
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct CapabilityDescriptor {
    id: String,
    description: String,
    input_schema: Map<String, Value>,
    #[serde(default)]
    output_schema: Option<Map<String, Value>>,
}

fn read_capability_descriptor(
    project_root: &Path,
    id: &str,
) -> Result<CapabilityDescriptor, String> {
    let path = project_root.join("capabilities").join(format!("{id}.yaml"));
    let metadata = fs::symlink_metadata(&path).map_err(|error| {
        format!(
            "cannot inspect the capability descriptor for {id:?} at '{}': {error}",
            path.display()
        )
    })?;
    if metadata.file_type().is_symlink() || !metadata.is_file() {
        return Err(format!(
            "capability descriptor for {id:?} is not a regular file: {}",
            path.display()
        ));
    }
    let source = fs::read_to_string(&path).map_err(|error| {
        format!(
            "cannot read the capability descriptor for {id:?} at '{}': {error}",
            path.display()
        )
    })?;
    let descriptor: CapabilityDescriptor = yaml_serde::from_str(&source).map_err(|error| {
        format!(
            "cannot parse the capability descriptor for {id:?} at '{}': {error}",
            path.display()
        )
    })?;
    if descriptor.id != id {
        return Err(format!(
            "capability descriptor at '{}' has id {:?}, expected {id:?}",
            path.display(),
            descriptor.id
        ));
    }
    if descriptor.description.trim().is_empty() {
        return Err(format!("capability descriptor {id:?} has no description"));
    }
    Ok(descriptor)
}

#[derive(Clone, Debug)]
struct IncludePath {
    root: PathBuf,
    recursive: bool,
}

impl IncludePath {
    fn parse(pattern: &str) -> Result<Self, String> {
        let (path, recursive) = match pattern.strip_suffix("/**") {
            Some(path) => (path, true),
            None => (pattern, false),
        };
        if path.is_empty() || path.contains('*') {
            return Err(format!("workspace include path is invalid: {pattern:?}"));
        }
        let root = PathBuf::from(path);
        if root.is_absolute()
            || root.components().any(|component| {
                matches!(
                    component,
                    Component::ParentDir | Component::RootDir | Component::Prefix(_)
                )
            })
        {
            return Err(format!("workspace include path is invalid: {pattern:?}"));
        }
        Ok(Self { root, recursive })
    }
}

fn collect_workspace_files(
    project_root: &Path,
    includes: &[IncludePath],
) -> Result<Vec<BundleFile>, String> {
    let mut files = BTreeMap::new();
    for include in includes {
        let path = project_root.join(&include.root);
        let metadata = match fs::symlink_metadata(&path) {
            Ok(metadata) => metadata,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
            Err(error) => {
                return Err(format!(
                    "cannot inspect workspace include '{}': {error}",
                    path.display()
                ));
            }
        };
        if metadata.file_type().is_symlink() {
            return Err(format!(
                "workspace include is a symbolic link: {}",
                path.display()
            ));
        }
        if metadata.is_file() {
            let file = read_bundle_file(project_root, &path)?;
            files.insert(file.path.clone(), file);
            continue;
        }
        if !metadata.is_dir() {
            return Err(format!(
                "workspace include is not a regular file or directory: {}",
                path.display()
            ));
        }
        if !include.recursive {
            return Err(format!(
                "workspace include must end with '/**' for a directory: {}",
                include.root.display()
            ));
        }
        collect_directory_files(project_root, &path, &mut files)?;
    }
    let files: Vec<_> = files.into_values().collect();
    bundle_size(&files)?;
    Ok(files)
}

fn collect_directory_files(
    project_root: &Path,
    directory: &Path,
    files: &mut BTreeMap<String, BundleFile>,
) -> Result<(), String> {
    let mut entries: Vec<_> = fs::read_dir(directory)
        .map_err(|error| {
            format!(
                "cannot read workspace directory '{}': {error}",
                directory.display()
            )
        })?
        .collect::<Result<_, _>>()
        .map_err(|error| format!("cannot read a workspace directory entry: {error}"))?;
    entries.sort_by_key(|entry| entry.file_name());
    for entry in entries {
        let path = entry.path();
        let file_type = entry.file_type().map_err(|error| {
            format!(
                "cannot inspect workspace file '{}': {error}",
                path.display()
            )
        })?;
        if file_type.is_symlink() {
            return Err(format!(
                "workspace file is a symbolic link: {}",
                path.display()
            ));
        }
        if file_type.is_dir() {
            collect_directory_files(project_root, &path, files)?;
        } else if file_type.is_file() {
            let file = read_bundle_file(project_root, &path)?;
            files.insert(file.path.clone(), file);
        } else {
            return Err(format!("workspace file is not regular: {}", path.display()));
        }
    }
    Ok(())
}

fn read_bundle_file(project_root: &Path, path: &Path) -> Result<BundleFile, String> {
    let metadata = fs::symlink_metadata(path)
        .map_err(|error| format!("cannot inspect bundle file '{}': {error}", path.display()))?;
    if metadata.file_type().is_symlink() || !metadata.is_file() {
        return Err(format!("bundle file is not regular: {}", path.display()));
    }
    let canonical = fs::canonicalize(path)
        .map_err(|error| format!("cannot resolve bundle file '{}': {error}", path.display()))?;
    let path = project_relative_path(project_root, &canonical)?;
    let bytes = fs::read(&canonical)
        .map_err(|error| format!("cannot read bundle file '{}': {error}", canonical.display()))?;
    Ok(BundleFile {
        path,
        content_address: hash_bytes(&bytes).to_string(),
        content_base64: base64::engine::general_purpose::STANDARD.encode(bytes),
    })
}

fn project_relative_path(project_root: &Path, path: &Path) -> Result<String, String> {
    let canonical_root = fs::canonicalize(project_root).map_err(|error| {
        format!(
            "cannot resolve the project root '{}': {error}",
            project_root.display()
        )
    })?;
    let canonical = fs::canonicalize(path)
        .map_err(|error| format!("cannot resolve bundle file '{}': {error}", path.display()))?;
    let relative = canonical.strip_prefix(&canonical_root).map_err(|_error| {
        format!(
            "bundle file is outside the project root: {}",
            canonical.display()
        )
    })?;
    let path = relative.to_str().ok_or_else(|| {
        format!(
            "bundle file path is not valid UTF-8: {}",
            relative.display()
        )
    })?;
    validate_bundle_path(path)?;
    Ok(path.to_owned())
}

fn validate_bundle_path(path: &str) -> Result<(), String> {
    let value = Path::new(path);
    if path.is_empty()
        || value.is_absolute()
        || value
            .components()
            .any(|component| !matches!(component, Component::Normal(_)))
    {
        return Err(format!("bundle file path is invalid: {path:?}"));
    }
    Ok(())
}

fn bundle_size(files: &[BundleFile]) -> Result<(), String> {
    let size = files.iter().try_fold(0usize, |total, file| {
        let decoded = base64::engine::general_purpose::STANDARD
            .decode(&file.content_base64)
            .map_err(|_error| "a bundle file has invalid base64 data".to_owned())?;
        total
            .checked_add(decoded.len())
            .ok_or_else(|| "workspace bundle is too large".to_owned())
    })?;
    if size > MAX_BUNDLE_BYTES {
        return Err(format!(
            "workspace bundle exceeds the {} byte IPC limit",
            MAX_BUNDLE_BYTES
        ));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use std::fs;

    use base64::Engine as _;
    use serde_json::json;
    use tempfile::TempDir;
    use zeta_manifest::parse_agent;

    use crate::{ExecutorProfile, PythonProviderCatalog};

    use super::{
        bundle_id, collect_workspace_files, BundleFile, ExecutorBundle, IncludePath,
        WorkspaceBundle,
    };

    #[test]
    fn bundle_identity_changes_with_file_bytes() {
        let first = vec![BundleFile {
            path: "tools/example.py".to_owned(),
            content_address: "b3:first".to_owned(),
            content_base64: "Zmlyc3Q=".to_owned(),
        }];
        let second = vec![BundleFile {
            path: "tools/example.py".to_owned(),
            content_address: "b3:second".to_owned(),
            content_base64: "c2Vjb25k".to_owned(),
        }];

        assert_ne!(
            bundle_id("workspace", &first).expect("first bundle id"),
            bundle_id("workspace", &second).expect("second bundle id")
        );
    }

    #[test]
    fn workspace_bundle_uses_only_included_regular_files() {
        let temporary = TempDir::new().expect("temporary project");
        fs::create_dir(temporary.path().join("tools")).expect("tools directory");
        fs::write(temporary.path().join("tools/example.py"), "print('ok')\n").expect("tool source");
        fs::write(temporary.path().join("ignored.txt"), "ignored\n").expect("ignored source");

        let files = collect_workspace_files(
            temporary.path(),
            &[IncludePath::parse("tools/**").expect("include path")],
        )
        .expect("workspace files");

        assert_eq!(files.len(), 1);
        assert_eq!(files[0].path, "tools/example.py");
        assert_eq!(
            base64::engine::general_purpose::STANDARD
                .decode(&files[0].content_base64)
                .expect("file bytes"),
            b"print('ok')\n"
        );
    }

    #[test]
    fn workspace_bundle_rejects_parent_path_patterns() {
        let error = IncludePath::parse("../tools/**").expect_err("parent path must fail");
        assert!(error.contains("invalid"));
    }

    #[test]
    fn executor_bundle_contains_verified_tool_source_and_schema() {
        let temporary = TempDir::new().expect("temporary project");
        let tools = temporary.path().join("tools");
        fs::create_dir(&tools).expect("tools directory");
        let source = tools.join("workspace.py");
        fs::write(&source, "def read():\n    return 'ok'\n").expect("tool source");
        let descriptors = temporary.path().join("capabilities");
        fs::create_dir(&descriptors).expect("capabilities directory");
        fs::write(
            descriptors.join("workspace.read.yaml"),
            "id: workspace.read\ndescription: Read one workspace file.\ninput_schema:\n  type: object\n  required: [path]\n  properties:\n    path:\n      type: string\noutput_schema:\n  type: object\n",
        )
        .expect("capability descriptor");
        let profile: ExecutorProfile = yaml_serde::from_str(
            "provider: fixture\nworkspace:\n  include: [\"tools/**\", \"capabilities/**\"]\n",
        )
        .expect("executor profile");
        let providers: PythonProviderCatalog = serde_json::from_value(json!({
            "models": {},
            "tools": {"workspace.read": {
                "id": "workspace.read",
                "source": {
                    "module": "zeta_project.tools.workspace",
                    "path": source,
                    "distribution": null
                },
                "fingerprint": "a".repeat(64),
                "description": "Read one workspace file.",
                "tool_profile": null,
                "input_schema": {
                    "type": "object",
                    "required": ["path"],
                    "properties": {"path": {"type": "string"}}
                },
                "output_schema": {"type": "object"}
            }},
            "connectors": {},
            "executors": {}
        }))
        .expect("provider catalog");
        let agent = parse_agent(
            "worker",
            b"---\nname: Worker\ndescription: Runs portable tools.\ntools: [workspace.read]\n---\nWork.\n",
        )
        .expect("agent");

        let workspace =
            WorkspaceBundle::build(temporary.path(), &profile).expect("workspace bundle");
        let bundle =
            ExecutorBundle::build_for_agent(temporary.path(), workspace, &providers, &agent)
                .expect("executor bundle");
        bundle.verify().expect("executor bundle verifies");
        let restored: ExecutorBundle = serde_json::from_value(
            serde_json::to_value(&bundle).expect("executor bundle serializes"),
        )
        .expect("executor bundle restores");
        restored
            .verify()
            .expect("restored executor bundle verifies");
        assert_eq!(restored, bundle);

        assert_eq!(bundle.workspace().files.len(), 2);
        assert_eq!(bundle.tools().files.len(), 1);
        assert_eq!(bundle.tools().capabilities.len(), 1);
        assert_eq!(bundle.tools().capabilities[0].id, "workspace.read");
        assert_eq!(
            bundle.tools().capabilities[0].input_schema["required"],
            json!(["path"])
        );

        fs::write(&source, "def read():\n    return 'changed'\n").expect("changed tool source");
        bundle
            .workspace()
            .verify()
            .expect("stored workspace bundle remains valid");
        let stored_source = bundle
            .workspace()
            .files
            .iter()
            .find(|file| file.path == "tools/workspace.py")
            .expect("stored tool source");
        assert_eq!(
            base64::engine::general_purpose::STANDARD
                .decode(&stored_source.content_base64)
                .expect("stored source bytes"),
            b"def read():\n    return 'ok'\n"
        );

        fs::write(
            descriptors.join("workspace.read.yaml"),
            "id: workspace.read\ndescription: Different description.\ninput_schema:\n  type: object\n  required: [path]\n  properties:\n    path:\n      type: string\noutput_schema:\n  type: object\n",
        )
        .expect("changed capability descriptor");
        let error = ExecutorBundle::build_for_agent(
            temporary.path(),
            bundle.workspace().clone(),
            &providers,
            &agent,
        )
        .expect_err("mismatched descriptor must fail");
        assert!(error.contains("does not match"));
    }
}
