//! Deterministic trace values returned for caller-owned persistence.

use serde::{Deserialize, Serialize};
use zeta_substrate::{Derivation, Object};

use crate::error::AgentError;

/// Pairs an immutable object with its content address.
#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
pub struct AddressedObject {
    /// Carries the object's current content address.
    pub id: String,
    /// Carries the complete immutable value.
    #[serde(flatten)]
    pub object: Object,
}

/// Pairs a provenance edge with its content address.
#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
pub struct AddressedDerivation {
    /// Carries the derivation's current content address.
    pub id: String,
    /// Carries the complete provenance edge.
    #[serde(flatten)]
    pub derivation: Derivation,
}

/// Returns deterministic trace values without selecting a substrate store.
#[derive(Clone, Debug, Default, Deserialize, PartialEq, Serialize)]
pub struct TraceBatch {
    /// Lists addressed objects in address order.
    pub objects: Vec<AddressedObject>,
    /// Lists addressed derivations in address order.
    pub derivations: Vec<AddressedDerivation>,
}

impl TraceBatch {
    pub(crate) fn insert_object(&mut self, object: Object) -> Result<String, AgentError> {
        let id = object
            .content_address()
            .map_err(|error| AgentError::trace(error.to_string()))?
            .to_string();
        let mut exists = false;
        for row in &self.objects {
            if row.id == id {
                exists = true;
                break;
            }
        }
        if !exists {
            self.objects.push(AddressedObject {
                id: id.clone(),
                object,
            });
            self.objects.sort_by(|left, right| left.id.cmp(&right.id));
        }
        Ok(id)
    }

    pub(crate) fn insert_derivation(
        &mut self,
        derivation: Derivation,
    ) -> Result<String, AgentError> {
        let id = derivation
            .content_address()
            .map_err(|error| AgentError::trace(error.to_string()))?
            .to_string();
        let mut exists = false;
        for row in &self.derivations {
            if row.id == id {
                exists = true;
                break;
            }
        }
        if !exists {
            self.derivations.push(AddressedDerivation {
                id: id.clone(),
                derivation,
            });
            self.derivations
                .sort_by(|left, right| left.id.cmp(&right.id));
        }
        Ok(id)
    }

    pub(crate) fn merge(&mut self, other: TraceBatch) -> Result<(), AgentError> {
        for AddressedObject { id: _id, object } in other.objects {
            self.insert_object(object)?;
        }
        for AddressedDerivation {
            id: _id,
            derivation,
        } in other.derivations
        {
            self.insert_derivation(derivation)?;
        }
        Ok(())
    }
}

/// Links one prompt request to its optional assistant response object.
#[derive(Clone, Debug, Deserialize, PartialEq, Eq, Serialize)]
pub struct PromptTrace {
    /// Identifies the complete prompt request.
    pub prompt_object_id: String,
    /// Identifies the response projected from the model proposal.
    pub assistant_message_object_id: Option<String>,
}
