import Foundation
import Security
import Citadel
import Crypto
import NIOSSH

struct SSHSecret: Codable { var privateKey: String; var passphrase: String }

enum KeyStore {
    private static var query: [String: Any] {
        [kSecClass as String: kSecClassGenericPassword,
         kSecAttrService as String: "com.example.ai-manager.ssh", kSecAttrAccount as String: "primary"]
    }
    static func save(_ secret: SSHSecret) throws {
        let data = try JSONEncoder().encode(secret)
        let attributes: [String: Any] = [kSecValueData as String: data,
            kSecAttrAccessible as String: kSecAttrAccessibleWhenUnlockedThisDeviceOnly]
        let result = SecItemUpdate(query as CFDictionary, attributes as CFDictionary)
        if result == errSecItemNotFound {
            guard SecItemAdd(query.merging(attributes) { _, new in new } as CFDictionary, nil) == errSecSuccess else { throw AIManagerError.keychain }
        } else if result != errSecSuccess { throw AIManagerError.keychain }
    }
    static func load() throws -> SSHSecret {
        var q = query
        q[kSecReturnData as String] = true
        q[kSecMatchLimit as String] = kSecMatchLimitOne
        var item: CFTypeRef?
        guard SecItemCopyMatching(q as CFDictionary, &item) == errSecSuccess, let data = item as? Data else { throw AIManagerError.keychain }
        return try JSONDecoder().decode(SSHSecret.self, from: data)
    }
    static func delete() { SecItemDelete(query as CFDictionary) }
}

enum SSHService {
    static func validateHostKey(_ value: String) throws {
        do { _ = try NIOSSHPublicKey(openSSHPublicKey: value) }
        catch { throw AIManagerError.hostKey }
    }
    static func authentication(username: String, secret: SSHSecret) throws -> SSHAuthenticationMethod {
        let pass = secret.passphrase.isEmpty ? nil : Data(secret.passphrase.utf8)
        if let key = try? Curve25519.Signing.PrivateKey(sshEd25519: secret.privateKey, decryptionKey: pass) {
            return .ed25519(username: username, privateKey: key)
        }
        if let key = try? Insecure.RSA.PrivateKey(sshRsa: secret.privateKey, decryptionKey: pass) {
            return .rsa(username: username, privateKey: key)
        }
        throw AIManagerError.key
    }

    static func connect(profile: ServerProfile) async throws -> SSHClient {
        try profile.validate()
        let secret = try KeyStore.load()
        let authentication = try authentication(username: profile.username, secret: secret)
        let hostKey: NIOSSHPublicKey
        do { hostKey = try NIOSSHPublicKey(openSSHPublicKey: profile.hostKey) }
        catch { throw AIManagerError.hostKey }
        var settings = SSHClientSettings(host: profile.host, port: Int(profile.port)!,
            authenticationMethod: { authentication }, hostKeyValidator: .trustedKeys([hostKey]))
        settings.connectTimeout = .seconds(15)
        do { return try await SSHClient.connect(to: settings) }
        catch is InvalidHostKey { throw AIManagerError.hostKey }
        catch is AuthenticationFailed { throw AIManagerError.authentication }
        catch { throw AIManagerError.connection }
    }

    static func execute(profile: ServerProfile, arguments: [String]) async throws -> Data {
        let client = try await connect(profile: profile)
        let watchdog = Task {
            try await Task.sleep(for: .seconds(30))
            try await client.close()
        }
        defer { watchdog.cancel() }
        do {
            let output = try await client.executeCommand(RemoteCommand.make(profile: profile, arguments: arguments),
                maxResponseSize: 2 * 1024 * 1024, mergeStreams: false)
            try? await client.close()
            return Data(output.readableBytesView)
        } catch {
            try? await client.close()
            throw AIManagerError.connection
        }
    }
}
