use zeta_manifest::CapabilityId;

#[test]
fn accepts_unqualified_and_namespaced_capability_ids() {
    let unqualified: CapabilityId = "web_search".parse().expect("valid tool id");
    let namespaced: CapabilityId = "pi.bash".parse().expect("valid tool id");

    assert_eq!(unqualified.as_str(), "web_search");
    assert_eq!(namespaced.as_str(), "pi.bash");
}

#[test]
fn rejects_invalid_capability_ids() {
    for value in ["", "WebSearch", "pi..bash", "pi-bash", "pi.bash! "] {
        assert!(
            value.parse::<CapabilityId>().is_err(),
            "{value:?} must fail"
        );
    }
}
