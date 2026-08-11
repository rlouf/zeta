//! RFC 3339 UTC timestamp validation (spec §3).
//!
//! Hand-rolled instead of pulling a date crate: the accepted grammar
//! is one fixed shape (`YYYY-MM-DDThh:mm:ss[.frac]Z`), and a small
//! validator keeps the dependency tree — and therefore the pinned
//! hash-stability surface — minimal.

pub fn is_valid_utc_timestamp(text: &str) -> bool {
    let bytes = text.as_bytes();
    if bytes.len() < 20 {
        return false;
    }
    let Some(rest) = text.strip_suffix('Z') else {
        return false;
    };
    let (base, fraction) = match rest.split_once('.') {
        Some((base, fraction)) => (base, Some(fraction)),
        None => (rest, None),
    };
    if let Some(fraction) = fraction {
        if fraction.is_empty() {
            return false;
        }
        for character in fraction.chars() {
            if !character.is_ascii_digit() {
                return false;
            }
        }
    }
    let base = base.as_bytes();
    if base.len() != 19 {
        return false;
    }
    if base[4] != b'-' || base[7] != b'-' || base[10] != b'T' {
        return false;
    }
    if base[13] != b':' || base[16] != b':' {
        return false;
    }
    let Some(year) = digits(&base[0..4]) else { return false };
    let Some(month) = digits(&base[5..7]) else { return false };
    let Some(day) = digits(&base[8..10]) else { return false };
    let Some(hour) = digits(&base[11..13]) else { return false };
    let Some(minute) = digits(&base[14..16]) else { return false };
    let Some(second) = digits(&base[17..19]) else { return false };
    if month < 1 || month > 12 {
        return false;
    }
    if day < 1 || day > days_in_month(year, month) {
        return false;
    }
    hour < 24 && minute < 60 && second < 60
}

fn digits(bytes: &[u8]) -> Option<u32> {
    let mut value = 0u32;
    for byte in bytes {
        if !byte.is_ascii_digit() {
            return None;
        }
        value = value * 10 + u32::from(byte - b'0');
    }
    Some(value)
}

fn days_in_month(year: u32, month: u32) -> u32 {
    match month {
        1 => 31,
        2 => {
            if year % 4 == 0 && (year % 100 != 0 || year % 400 == 0) {
                29
            } else {
                28
            }
        }
        3 => 31,
        4 => 30,
        5 => 31,
        6 => 30,
        7 => 31,
        8 => 31,
        9 => 30,
        10 => 31,
        11 => 30,
        12 => 31,
        other => {
            let _ = other;
            0
        }
    }
}

#[cfg(test)]
mod tests {
    use super::is_valid_utc_timestamp;

    #[test]
    fn accepts_the_spec_shapes() {
        assert!(is_valid_utc_timestamp("2026-08-10T12:00:00Z"));
        assert!(is_valid_utc_timestamp("2026-08-10T12:00:00.123Z"));
        assert!(is_valid_utc_timestamp("2024-02-29T00:00:00Z"));
    }

    #[test]
    fn rejects_offsets_calendar_lies_and_garbage() {
        assert!(!is_valid_utc_timestamp("2026-08-10T14:00:00+02:00"));
        assert!(!is_valid_utc_timestamp("10 Aug 2026 12:00"));
        assert!(!is_valid_utc_timestamp("2026-13-10T12:00:00Z"));
        assert!(!is_valid_utc_timestamp("2026-02-30T12:00:00Z"));
        assert!(!is_valid_utc_timestamp("2023-02-29T12:00:00Z"));
        assert!(!is_valid_utc_timestamp("2026-08-10T24:00:00Z"));
        assert!(!is_valid_utc_timestamp("2026-08-10T12:00:00."));
        assert!(!is_valid_utc_timestamp("2026-08-10T12:00:00.Z"));
        assert!(!is_valid_utc_timestamp(""));
    }
}
