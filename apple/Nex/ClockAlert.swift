import Foundation

struct ClockAlert: Decodable, Equatable, Identifiable {
    enum Kind: String, Decodable {
        case timer, alarm

        var symbol: String { self == .timer ? "timer" : "alarm" }
    }

    enum Status: String, Decodable {
        case scheduled, fired, cancelled
    }

    let id: String
    let kind: Kind
    let label: String
    let createdAt: String
    let dueAt: String
    let zone: String
    let status: Status
    var iphoneAlarmScheduled: Bool? = nil

    var due: Date? { WorkspaceDate.instant(dueAt) }

    func status(at date: Date) -> Status {
        if status == .scheduled, let due, due <= date { return .fired }
        return status
    }

    static func groupedByCompletion(_ alerts: [Self], at date: Date) -> (scheduled: [Self], completed: [Self]) {
        var scheduled: [Self] = []
        var completed: [Self] = []
        for alert in alerts {
            if alert.status(at: date) == .scheduled {
                scheduled.append(alert)
            } else {
                completed.append(alert)
            }
        }
        return (scheduled, completed)
    }
}

struct GoalOverview: Decodable {
    let items: [NexGoal]
    let clockAlerts: [ClockAlert]?
}
