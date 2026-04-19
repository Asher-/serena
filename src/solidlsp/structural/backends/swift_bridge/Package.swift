// swift-tools-version:5.10
// SwiftPM manifest for the serena-swift-bridge executable.
//
// The bridge speaks length-prefixed JSON over stdio so the Python
// `SwiftStructuralLanguage` backend can drive SwiftSyntax parse, walk, and
// mutation operations without requiring a Python-side Swift FFI.
//
// SwiftSyntax majors track Swift toolchain majors (e.g. 602.x pairs with
// Swift 6.2). The backend is developed against Swift 6.2.
import PackageDescription

let package = Package(
    name: "serena-swift-bridge",
    platforms: [.macOS(.v13)],
    products: [
        .executable(name: "serena-swift-bridge", targets: ["serena-swift-bridge"])
    ],
    dependencies: [
        .package(
            url: "https://github.com/swiftlang/swift-syntax.git",
            from: "600.0.0"
        )
    ],
    targets: [
        .executableTarget(
            name: "serena-swift-bridge",
            dependencies: [
                .product(name: "SwiftSyntax", package: "swift-syntax"),
                .product(name: "SwiftParser", package: "swift-syntax"),
                .product(name: "SwiftSyntaxBuilder", package: "swift-syntax"),
            ]
        )
    ]
)
