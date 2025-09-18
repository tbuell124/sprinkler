import Foundation

/// Lightweight HTTP client responsible for communicating with the
/// Sprinkler controller. The client conditionally installs the
/// SSLPinningDelegate only for HTTPS endpoints so that development
/// scenarios using HTTP (e.g., when testing on a LAN with a proxy)
/// continue to work without additional configuration.
final class SprinklerAPIClient {
    /// Base URL for the sprinkler API. The path component is expected to
    /// point at the controller's root endpoint.
    private(set) var baseURL: URL

    /// Underlying URLSession used for all network calls.
    private var session: URLSession

    /// Strong reference to the delegate so it stays alive for the lifetime
    /// of the session. Optional because we only instantiate it for HTTPS
    /// URLs.
    private var pinningDelegate: SSLPinningDelegate?

    /// Construct a new API client.
    ///
    /// - Parameters:
    ///   - baseURL: Target base URL. The scheme determines whether SSL
    ///     pinning is activated.
    ///   - configuration: Optional URLSession configuration. `.ephemeral`
    ///     is a sensible default for authenticated APIs because it avoids
    ///     persisting caches or cookies.
    ///   - pinnedCertificates: Optional DER-encoded certificates whose
    ///     SPKI hashes will be trusted by the SSLPinningDelegate.
    init(
        baseURL: URL,
        configuration: URLSessionConfiguration = .ephemeral,
        pinnedCertificates: [Data] = []
    ) {
        self.baseURL = baseURL

        if baseURL.scheme?.lowercased() == "https" {
            let delegate = SSLPinningDelegate(baseURL: baseURL, pinnedCertificates: pinnedCertificates)
            self.pinningDelegate = delegate
            self.session = URLSession(configuration: configuration, delegate: delegate, delegateQueue: nil)
        } else {
            // No pinning delegate for non-HTTPS endpoints. We still rely on
            // the default session behaviour so development servers without
            // TLS remain accessible.
            self.session = URLSession(configuration: configuration)
            self.pinningDelegate = nil
        }
    }

    /// Update the base URL and rebuild the session if the scheme changes.
    /// This ensures that switching from HTTP to HTTPS (or vice versa)
    /// adjusts SSL pinning behaviour accordingly.
    func updateBaseURL(
        _ url: URL,
        pinnedCertificates: [Data] = [],
        configuration: URLSessionConfiguration = .ephemeral
    ) {
        baseURL = url
        if url.scheme?.lowercased() == "https" {
            let delegate = pinningDelegate ?? SSLPinningDelegate(baseURL: url, pinnedCertificates: pinnedCertificates)
            delegate.updatePinnedCertificates(pinnedCertificates)
            pinningDelegate = delegate
            session.invalidateAndCancel()
            session = URLSession(configuration: configuration, delegate: delegate, delegateQueue: nil)
        } else {
            session.invalidateAndCancel()
            session = URLSession(configuration: configuration)
            pinningDelegate = nil
        }
    }

    /// Execute a GET request and return the decoded response. The method
    /// demonstrates how consumers can use the client while keeping the
    /// network layer fully testable.
    func get<T: Decodable>(_ path: String, as type: T.Type) async throws -> T {
        let requestURL = baseURL.appendingPathComponent(path)
        var request = URLRequest(url: requestURL)
        request.httpMethod = "GET"

        let (data, response) = try await session.data(for: request)
        guard let httpResponse = response as? HTTPURLResponse,
              200..<300 ~= httpResponse.statusCode else {
            throw URLError(.badServerResponse)
        }
        return try JSONDecoder().decode(T.self, from: data)
    }
}
