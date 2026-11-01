import XCTest
@testable import ZippieCompanionKit

/// The table every reader of Carrying runs, in every language (#369).
///
/// The agent decides and publishes `contributing`. This app displays it. A row
/// that cannot decode at all has no verdict, so it reads as not carrying.
final class LegVerdictVectorTests: XCTestCase {

    private struct Vector {
        let why: String
        let leg: [String: Any]
        let carrying: Bool
    }

    private func vectors() throws -> [Vector] {
        let root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
        let file = root.appendingPathComponent(
            "travel/bond-agent/tests/fixtures/leg_verdict_vectors.json")
        let rows = try JSONSerialization.jsonObject(with: Data(contentsOf: file))
        return try XCTUnwrap(rows as? [[String: Any]]).map {
            Vector(why: try XCTUnwrap($0["why"] as? String),
                   leg: try XCTUnwrap($0["leg"] as? [String: Any]),
                   carrying: try XCTUnwrap($0["carrying"] as? Bool))
        }
    }

    func testEveryVectorReadsTheSameHere() throws {
        let table = try vectors()
        XCTAssertFalse(table.isEmpty)
        for vector in table {
            var row = vector.leg
            row["name"] = "leg"
            row["state"] = "up"
            let data = try JSONSerialization.data(withJSONObject: row)
            let path = try? JSONDecoder().decode(BondStatus.Path.self, from: data)
            XCTAssertEqual(path?.isCarrying ?? false, vector.carrying, vector.why)
        }
    }
}
