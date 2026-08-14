//! Durable storage for immutable substrate trace values.

use rusqlite::{OptionalExtension, params};
use zeta_substrate::{Derivation, Object};

use super::{Dispatch, DispatchError, database_error};

impl Dispatch {
    /// Stores immutable trace values after validating their content addresses.
    ///
    /// Existing values with the same address remain unchanged.
    ///
    /// # Errors
    ///
    /// Returns [`DispatchError`] when an address is invalid or SQLite cannot
    /// store a trace value.
    pub fn persist_trace(
        &mut self,
        objects: &[(&str, &Object)],
        derivations: &[(&str, &Derivation)],
    ) -> Result<(), DispatchError> {
        let transaction = self
            .connection
            .transaction()
            .map_err(|error| database_error("begin trace persistence", error))?;
        for (identifier, object) in objects {
            let address = object.content_address().map_err(|_error| DispatchError::InvalidTrace {
                identifier: (*identifier).to_owned(),
                field: "object",
            })?;
            if address.to_string() != *identifier {
                return Err(DispatchError::InvalidTrace {
                    identifier: (*identifier).to_owned(),
                    field: "object_id",
                });
            }
            let bytes = object.canonical_bytes().map_err(|_error| DispatchError::InvalidTrace {
                identifier: (*identifier).to_owned(),
                field: "object",
            })?;
            transaction
                .execute(
                    "INSERT INTO substrate_objects (object_id, object_json)
                     VALUES (?1, ?2)
                     ON CONFLICT(object_id) DO NOTHING",
                    params![identifier, bytes],
                )
                .map_err(|error| database_error("store trace object", error))?;
        }
        for (identifier, derivation) in derivations {
            let address = derivation
                .content_address()
                .map_err(|_error| DispatchError::InvalidTrace {
                    identifier: (*identifier).to_owned(),
                    field: "derivation",
                })?;
            if address.to_string() != *identifier {
                return Err(DispatchError::InvalidTrace {
                    identifier: (*identifier).to_owned(),
                    field: "derivation_id",
                });
            }
            let bytes = derivation
                .canonical_bytes()
                .map_err(|_error| DispatchError::InvalidTrace {
                    identifier: (*identifier).to_owned(),
                    field: "derivation",
                })?;
            transaction
                .execute(
                    "INSERT INTO substrate_derivations
                     (derivation_id, output_id, derivation_json)
                     VALUES (?1, ?2, ?3)
                     ON CONFLICT(derivation_id) DO NOTHING",
                    params![identifier, derivation.output_id, bytes],
                )
                .map_err(|error| database_error("store trace derivation", error))?;
        }
        transaction
            .commit()
            .map_err(|error| database_error("commit trace persistence", error))?;
        Ok(())
    }

    /// Returns one immutable trace object by its content address.
    ///
    /// # Errors
    ///
    /// Returns [`DispatchError`] when SQLite cannot read or validate the value.
    pub fn trace_object(&self, identifier: &str) -> Result<Option<Object>, DispatchError> {
        let bytes = self
            .connection
            .query_row(
                "SELECT object_json FROM substrate_objects WHERE object_id = ?1",
                params![identifier],
                |row| row.get::<_, Vec<u8>>(0),
            )
            .optional()
            .map_err(|error| database_error("read trace object", error))?;
        let Some(bytes) = bytes else {
            return Ok(None);
        };
        let object = serde_json::from_slice::<Object>(&bytes).map_err(|_error| {
            DispatchError::CorruptTrace {
                identifier: identifier.to_owned(),
                field: "object_json",
            }
        })?;
        let address = object.content_address().map_err(|_error| DispatchError::CorruptTrace {
            identifier: identifier.to_owned(),
            field: "object_json",
        })?;
        if address.to_string() != identifier {
            return Err(DispatchError::CorruptTrace {
                identifier: identifier.to_owned(),
                field: "object_id",
            });
        }
        Ok(Some(object))
    }

    /// Returns one immutable derivation by its content address.
    ///
    /// # Errors
    ///
    /// Returns [`DispatchError`] when SQLite cannot read or validate the value.
    pub fn trace_derivation(
        &self,
        identifier: &str,
    ) -> Result<Option<Derivation>, DispatchError> {
        let bytes = self
            .connection
            .query_row(
                "SELECT derivation_json FROM substrate_derivations
                 WHERE derivation_id = ?1",
                params![identifier],
                |row| row.get::<_, Vec<u8>>(0),
            )
            .optional()
            .map_err(|error| database_error("read trace derivation", error))?;
        let Some(bytes) = bytes else {
            return Ok(None);
        };
        let derivation = serde_json::from_slice::<Derivation>(&bytes).map_err(|_error| {
            DispatchError::CorruptTrace {
                identifier: identifier.to_owned(),
                field: "derivation_json",
            }
        })?;
        let address = derivation
            .content_address()
            .map_err(|_error| DispatchError::CorruptTrace {
                identifier: identifier.to_owned(),
                field: "derivation_json",
            })?;
        if address.to_string() != identifier {
            return Err(DispatchError::CorruptTrace {
                identifier: identifier.to_owned(),
                field: "derivation_id",
            });
        }
        Ok(Some(derivation))
    }
}

#[cfg(test)]
mod tests {
    use serde_json::json;
    use zeta_substrate::{Derivation, Object};

    use super::Dispatch;

    #[test]
    fn persists_and_rehydrates_immutable_trace_values() {
        let object = Object {
            kind: "test.message".to_owned(),
            schema: "test.message.v1".to_owned(),
            data: serde_json::from_value(json!({"text": "hello"})).expect("object data"),
            links: Vec::new(),
        };
        let object_id = object.content_address().expect("object address").to_string();
        let derivation = Derivation {
            producer: "test".to_owned(),
            output_id: object_id.clone(),
            input_ids: Vec::new(),
            params: serde_json::from_value(json!({})).expect("derivation params"),
        };
        let derivation_id = derivation
            .content_address()
            .expect("derivation address")
            .to_string();
        let mut dispatch = Dispatch::open_in_memory().expect("dispatch");

        dispatch
            .persist_trace(
                &[(object_id.as_str(), &object)],
                &[(derivation_id.as_str(), &derivation)],
            )
            .expect("trace persistence");

        assert_eq!(
            dispatch.trace_object(&object_id).expect("trace object"),
            Some(object)
        );
        assert_eq!(
            dispatch
                .trace_derivation(&derivation_id)
                .expect("trace derivation"),
            Some(derivation)
        );
    }
}
