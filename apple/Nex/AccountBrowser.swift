import AuthenticationServices
#if os(macOS)
import AppKit
#else
import UIKit
#endif

@MainActor
final class AccountBrowser: NSObject, ASWebAuthenticationPresentationContextProviding {
    private var window: ASPresentationAnchor?
    private var session: ASWebAuthenticationSession?
    private var continuation: CheckedContinuation<URL, Error>?

    func authenticate(_ url: URL, scheme: String) async throws -> URL {
        try Task.checkCancellation()
        #if os(macOS)
        window = NSApp.keyWindow
        #else
        window = UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }
            .first { $0.activationState == .foregroundActive }?.keyWindow
        #endif
        guard window != nil else { throw AccountSignInError.browserUnavailable }
        return try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { continuation in
                self.continuation = continuation
                let completed: ASWebAuthenticationSession.CompletionHandler = { [weak self] url, error in
                    Task { @MainActor in
                        if let url { self?.complete(.success(url)) }
                        else if (error as? ASWebAuthenticationSessionError)?.code == .canceledLogin {
                            self?.complete(.failure(AccountSignInError.browserClosed))
                        } else {
                            self?.complete(.failure(error ?? AccountSignInError.browserUnavailable))
                        }
                    }
                }
                let browser: ASWebAuthenticationSession
                if #available(iOS 17.4, macOS 14.4, *) {
                    browser = ASWebAuthenticationSession(url: url, callback: .customScheme(scheme), completionHandler: completed)
                } else {
                    browser = ASWebAuthenticationSession(url: url, callbackURLScheme: scheme, completionHandler: completed)
                }
                browser.presentationContextProvider = self
                session = browser
                if !browser.start() { complete(.failure(AccountSignInError.browserUnavailable)) }
            }
        } onCancel: {
            Task { @MainActor in
                self.session?.cancel()
                self.complete(.failure(CancellationError()))
            }
        }
    }

    func presentationAnchor(for session: ASWebAuthenticationSession) -> ASPresentationAnchor {
        window ?? ASPresentationAnchor()
    }

    private func complete(_ outcome: Result<URL, Error>) {
        let pending = continuation
        continuation = nil
        session = nil
        window = nil
        pending?.resume(with: outcome)
    }
}
