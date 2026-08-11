//! A content-addressed blob store over a directory.
//!
//! Writes go through a temporary file and a rename so a crash never
//! leaves a half-written blob under a valid name. Reads can verify
//! the hash on every byte returned, because a store that silently
//! serves corrupted content defeats the point of content addressing.
//! Blob paths use two digest characters as a fanout directory so directory
//! listing and synchronization costs stay bounded as a store grows.

use std::fs;
use std::io;
use std::path::{Path, PathBuf};

use super::hash::{hash_bytes, Hash};

/// A content-addressed blob store rooted at one directory.
///
/// # Examples
///
/// ```
/// use zeta::substrate::BlobStore;
///
/// let root = tempfile::tempdir().unwrap();
/// let store = BlobStore::new(root.path());
/// let hash = store.put(b"payload bytes").unwrap();
/// assert_eq!(store.read_verified(&hash).unwrap(), b"payload bytes");
/// ```
pub struct BlobStore {
    root: PathBuf,
}

impl BlobStore {
    /// Creates a store over `root` without touching the filesystem.
    ///
    /// Directories appear on first write, so constructing a store is
    /// free and read-only stores need no permissions they do not use.
    ///
    /// # Examples
    ///
    /// ```
    /// let root = tempfile::tempdir().unwrap();
    /// let store = zeta::substrate::BlobStore::new(root.path());
    /// let hash = zeta::substrate::hash_bytes(b"payload");
    /// assert!(store.path_of(&hash).starts_with(root.path()));
    /// ```
    pub fn new(root: &Path) -> Self {
        BlobStore {
            root: root.to_path_buf(),
        }
    }

    /// Returns the path where a blob with this hash lives.
    ///
    /// # Examples
    ///
    /// ```
    /// use std::path::Path;
    /// use zeta::substrate::BlobStore;
    ///
    /// let store = BlobStore::new(Path::new("/store"));
    /// let hash = zeta::substrate::hash_bytes(b"x");
    /// let path = store.path_of(&hash);
    /// assert!(path.starts_with("/store/blobs"));
    /// ```
    pub fn path_of(&self, hash: &Hash) -> PathBuf {
        let hex = hash.to_hex();
        let (fanout, rest) = hex.split_at(2);
        self.root.join("blobs").join(fanout).join(rest)
    }

    /// Stores bytes under their content address and returns it.
    ///
    /// The write is idempotent: a blob that already exists is left
    /// untouched, because equal addresses guarantee equal bytes. New
    /// blobs land through a temporary file in the destination
    /// directory plus a rename, so readers never observe a partial
    /// blob under a valid name.
    ///
    /// # Errors
    ///
    /// Returns [`io::Error`] if directories cannot be created or the
    /// write fails.
    ///
    /// [`io::Error`]: std::io::Error
    pub fn put(&self, bytes: &[u8]) -> io::Result<Hash> {
        let hash = hash_bytes(bytes);
        let path = self.path_of(&hash);
        if path.is_file() {
            return Ok(hash);
        }
        let Some(parent) = path.parent() else {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "blob path has no parent directory",
            ));
        };
        fs::create_dir_all(parent)?;
        let mut temporary = tempfile::NamedTempFile::new_in(parent)?;
        io::Write::write_all(&mut temporary, bytes)?;
        let outcome = temporary.persist(&path);
        let Err(error) = outcome else {
            return Ok(hash);
        };
        if path.is_file() {
            return Ok(hash);
        }
        Err(error.error)
    }

    /// Reads a blob's bytes without verification.
    ///
    /// # Errors
    ///
    /// Returns [`io::Error`] if the blob is absent or unreadable.
    ///
    /// [`io::Error`]: std::io::Error
    pub fn get(&self, hash: &Hash) -> io::Result<Vec<u8>> {
        fs::read(self.path_of(hash))
    }

    /// Reads a blob's bytes and verifies them against the address.
    ///
    /// Every read re-hashes, because content addressing only means
    /// something if corruption cannot pass silently.
    ///
    /// # Errors
    ///
    /// Returns [`io::Error`] with kind [`InvalidData`] naming the
    /// blob when the stored bytes do not hash to the address, and
    /// any underlying read error otherwise.
    ///
    /// [`io::Error`]: std::io::Error
    /// [`InvalidData`]: std::io::ErrorKind::InvalidData
    pub fn read_verified(&self, hash: &Hash) -> io::Result<Vec<u8>> {
        let bytes = self.get(hash)?;
        let actual = hash_bytes(&bytes);
        if actual != *hash {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                format!("blob {hash} is corrupt: stored bytes hash to {actual}"),
            ));
        }
        Ok(bytes)
    }
}
