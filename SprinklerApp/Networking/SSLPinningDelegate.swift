import Foundation
import CommonCrypto

/// A URLSessionDelegate that performs SSL pinning using the server's
/// public key hash (SPKI).  Pinning the SPKI rather than the full
/// certificate allows the backend to rotate certificates without
/// breaking the client, as long as the underlying key pair remains the
/// same.
final class SSLPinningDelegate: NSObject, URLSessionDelegate {
    /// Expected host name for the session.  The delegate rejects
    /// challenges for any other host to guard against man-in-the-middle
    /// attempts that rely on a different certificate chain.
    private let expectedHost: String

    /// Set of allowed SPKI hashes. Multiple hashes are supported so we
    /// can stage new certificates during rotation.
    private var allowedSPKIHashes: Set<Data>

    /// Initialize the delegate with the expected host and an optional
    /// list of pinned certificates (DER encoded).
    ///
    /// - Parameters:
    ///   - baseURL: Base URL used by the client. The host component is
    ///     extracted and used for trust evaluation.
    ///   - pinnedCertificates: Raw DER encoded certificates that should
    ///     be trusted. If empty, the delegate will fall back to the
    ///     system trust chain.
    init(baseURL: URL, pinnedCertificates: [Data]) {
        self.expectedHost = baseURL.host?.lowercased() ?? ""
        self.allowedSPKIHashes = Set(
            pinnedCertificates.compactMap { SSLPinningDelegate.hashForCertificateData($0) }
        )
        super.init()
    }

    /// Allows callers to update the pinned certificates at runtime.
    /// This is useful during phased certificate rotations where the app
    /// may fetch new certificates from a trusted channel and update the
    /// delegate without recreating the session.
    func updatePinnedCertificates(_ certificates: [Data]) {
        allowedSPKIHashes = Set(
            certificates.compactMap { SSLPinningDelegate.hashForCertificateData($0) }
        )
    }

    func urlSession(
        _ session: URLSession,
        didReceive challenge: URLAuthenticationChallenge,
        completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void
    ) {
        guard challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust else {
            // For non-server trust challenges, defer to default handling.
            completionHandler(.performDefaultHandling, nil)
            return
        }

        guard let serverTrust = challenge.protectionSpace.serverTrust else {
            completionHandler(.cancelAuthenticationChallenge, nil)
            return
        }

        // Enforce hostname verification by ensuring the challenge host
        // matches the base URL host exactly (case-insensitive).
        let challengeHost = challenge.protectionSpace.host.lowercased()
        guard challengeHost == expectedHost else {
            completionHandler(.cancelAuthenticationChallenge, nil)
            return
        }

        // Apply SSL policy for the expected host so the system validates
        // the certificate chain and hostname.
        SecTrustSetPolicies(serverTrust, SecPolicyCreateSSL(true, expectedHost as CFString))

        // Evaluate the trust chain using the modern API. If it fails we
        // immediately cancel the challenge.
        guard SecTrustEvaluateWithError(serverTrust, nil) else {
            completionHandler(.cancelAuthenticationChallenge, nil)
            return
        }

        // When no pins are configured we rely on the default system
        // trust. This allows environments where we only want hostname
        // validation but not strict pinning.
        guard !allowedSPKIHashes.isEmpty else {
            completionHandler(.useCredential, URLCredential(trust: serverTrust))
            return
        }

        // Extract the public key hashes from the presented certificate
        // chain and compare them against the allowed hashes. We accept the
        // challenge if any hash matches, which enables graceful
        // certificate rotation by staging new certificates ahead of time.
        let serverHashes = SSLPinningDelegate.hashesForServerTrust(serverTrust)
        let intersection = serverHashes.intersection(allowedSPKIHashes)
        guard !intersection.isEmpty else {
            completionHandler(.cancelAuthenticationChallenge, nil)
            return
        }

        completionHandler(.useCredential, URLCredential(trust: serverTrust))
    }
}

private extension SSLPinningDelegate {
    /// Compute SPKI hashes for every certificate in the provided trust.
    static func hashesForServerTrust(_ trust: SecTrust) -> Set<Data> {
        var hashes: Set<Data> = []
        let certificateCount = SecTrustGetCertificateCount(trust)
        for index in 0..<certificateCount {
            if let certificate = SecTrustGetCertificateAtIndex(trust, index),
               let hash = hashForCertificate(certificate) {
                hashes.insert(hash)
            }
        }
        return hashes
    }

    /// Calculate the SPKI hash for a single certificate reference.
    static func hashForCertificate(_ certificate: SecCertificate) -> Data? {
        guard let key = SecCertificateCopyKey(certificate) else {
            return nil
        }
        var error: Unmanaged<CFError>?
        guard let publicKeyData = SecKeyCopyExternalRepresentation(key, &error) as Data? else {
            return nil
        }
        return sha256(publicKeyData)
    }

    /// Calculate the SPKI hash from raw certificate data.
    static func hashForCertificateData(_ data: Data) -> Data? {
        guard let certificate = SecCertificateCreateWithData(nil, data as CFData) else {
            return nil
        }
        return hashForCertificate(certificate)
    }

    /// Helper that performs SHA-256 hashing using CommonCrypto.
    static func sha256(_ data: Data) -> Data {
        var hash = Data(count: Int(CC_SHA256_DIGEST_LENGTH))
        _ = hash.withUnsafeMutableBytes { hashBytes in
            data.withUnsafeBytes { dataBytes in
                CC_SHA256(dataBytes.baseAddress, CC_LONG(data.count), hashBytes.bindMemory(to: UInt8.self).baseAddress)
            }
        }
        return hash
    }
}
