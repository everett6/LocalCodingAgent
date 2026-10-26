//! Optional ASCII token feature hashing. No filesystem, network or retained state.
use blake2::digest::{Update, VariableOutput};
use blake2::Blake2bVar;

fn counts(tokens: &[u8]) -> Option<[f64; 256]> {
    if !tokens.is_ascii() || tokens.len() > 16 * 1024 * 1024 {
        return None;
    }
    let mut vector = [0.0; 256];
    let mut feature = |bytes: &[u8]| {
        let mut hash = Blake2bVar::new(4).expect("constant digest length");
        hash.update(bytes);
        let mut digest = [0u8; 4];
        hash.finalize_variable(&mut digest).expect("constant output length");
        vector[digest[1] as usize] += if digest[2] & 1 == 1 { 1.0 } else { -1.0 };
    };
    for token in tokens.split(|b| *b == b'\n').filter(|t| !t.is_empty()) {
        feature(token);
        for trigram in token.windows(3) {
            feature(trigram);
        }
    }
    Some(vector)
}

/// ABI version checked before binding the hashing entry point.
#[no_mangle]
pub extern "C" fn local_coder_abi_version() -> u32 {
    1
}

/// Hash newline-separated ASCII tokens into a caller-owned 256-double buffer.
///
/// # Safety
/// `input` must point to `length` readable bytes (or be null for zero length).
/// `output` must point to 256 writable, aligned doubles, disjoint from `input`.
/// The function retains neither pointer. Returns nonzero for invalid parameters.
#[no_mangle]
pub unsafe extern "C" fn local_coder_lexical_counts(
    input: *const u8, length: usize, output: *mut f64, output_length: usize,
) -> i32 {
    if output.is_null() || output_length != 256 || length > 16 * 1024 * 1024 || (input.is_null() && length != 0) {
        return 1;
    }
    let tokens = if length == 0 { &[] } else { std::slice::from_raw_parts(input, length) };
    let Some(vector) = counts(tokens) else { return 2; };
    std::ptr::copy_nonoverlapping(vector.as_ptr(), output, 256);
    0
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn empty_and_invalid_inputs() {
        assert_eq!(counts(b"").unwrap(), [0.0; 256]);
        assert!(counts("界".as_bytes()).is_none());
        unsafe {
            assert_ne!(local_coder_lexical_counts(std::ptr::null(), 1, std::ptr::null_mut(), 0), 0);
        }
    }

    #[test]
    fn repeated_tokens_accumulate_without_retained_state() {
        let once = counts(b"parser").unwrap();
        let twice = counts(b"parser\nparser").unwrap();
        assert!(once.iter().zip(twice).all(|(a, b)| *a * 2.0 == b));
        assert_eq!(counts(b"parser").unwrap(), once);
    }
}
