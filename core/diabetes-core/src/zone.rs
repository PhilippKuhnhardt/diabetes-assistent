//! Local clocks: the home zone, or the travel zone once the user switched zones (app/common.py
//! TravelZone). From each switch on, local clock times (meal kinds, KF schedule, day boundaries) are
//! read in the zone in effect; flying east skips local hours, flying home repeats them (the second
//! pass of a repeated hour is the later instant, as with daylight saving time).

use crate::model::Instant;
use chrono::{Duration, LocalResult, NaiveDateTime, Offset, TimeZone, Timelike};
use chrono_tz::Tz;

fn tz(name: &str) -> Tz {
    name.parse::<Tz>().unwrap_or(chrono_tz::UTC)
}

fn wall_of(zone: Tz, utc: NaiveDateTime) -> NaiveDateTime {
    zone.from_utc_datetime(&utc).naive_local()
}

fn offset_seconds(zone: Tz, utc: NaiveDateTime) -> i64 {
    zone.offset_from_utc_datetime(&utc).fix().local_minus_utc() as i64
}

/// UTC of a wall time in one zone: a repeated time takes its first (`later` false) or second
/// instant, a skipped one is read with the offset before (or after) the jump.
fn zone_utc(zone: Tz, wall: NaiveDateTime, later: bool) -> NaiveDateTime {
    match zone.from_local_datetime(&wall) {
        LocalResult::Single(at) => at.naive_utc(),
        LocalResult::Ambiguous(a, b) => {
            let (early, late) = if a.naive_utc() <= b.naive_utc() { (a, b) } else { (b, a) };
            if later { late.naive_utc() } else { early.naive_utc() }
        }
        LocalResult::None => {
            let before = offset_seconds(zone, wall - Duration::hours(12));
            let after = offset_seconds(zone, wall + Duration::hours(12));
            wall - Duration::seconds(if later { after } else { before })
        }
    }
}

#[derive(Clone, Debug)]
pub struct Zone {
    zones: Vec<Tz>,
    starts: Vec<NaiveDateTime>,
}

impl Zone {
    pub fn new(home: &str, switches: &[(Instant, String)]) -> Zone {
        let mut zones = vec![tz(home)];
        zones.extend(switches.iter().map(|(_, name)| tz(if name.is_empty() { home } else { name })));
        Zone { zones, starts: switches.iter().map(|(at, _)| at.naive_utc()).collect() }
    }

    fn index(&self, utc: NaiveDateTime) -> usize {
        self.starts.partition_point(|start| *start <= utc)
    }

    /// IANA name of the zone in effect at an instant.
    pub fn name_at(&self, at: &Instant) -> String {
        self.zones[self.index(at.naive_utc())].name().to_string()
    }

    /// The wall clock at an instant.
    pub fn local(&self, at: &Instant) -> NaiveDateTime {
        let utc = at.naive_utc();
        wall_of(self.zones[self.index(utc)], utc)
    }

    /// Minutes after local midnight at an instant.
    pub fn minute_of_day(&self, at: &Instant) -> i64 {
        let local = self.local(at);
        (local.hour() * 60 + local.minute()) as i64
    }

    fn instants(&self, wall: NaiveDateTime) -> Vec<NaiveDateTime> {
        let mut found: Vec<NaiveDateTime> = Vec::new();
        for (index, zone) in self.zones.iter().enumerate() {
            let low = if index > 0 { Some(self.starts[index - 1]) } else { None };
            let high = self.starts.get(index).copied();
            for later in [false, true] {
                let utc = zone_utc(*zone, wall, later);
                if low.is_none_or(|low| utc >= low) && high.is_none_or(|high| utc < high)
                    && wall_of(*zone, utc) == wall && !found.contains(&utc) {
                    found.push(utc);
                }
            }
        }
        found.sort();
        found
    }

    /// The (first) instant whose wall clock reads `wall`; a skipped time keeps the clock before the switch.
    pub fn from_local(&self, wall: NaiveDateTime) -> Instant {
        let utc = if self.starts.is_empty() {
            zone_utc(self.zones[0], wall, false)
        } else if let Some(first) = self.instants(wall).first() {
            *first
        } else {
            self.starts.iter().enumerate()
                .find(|(index, start)| wall_of(self.zones[*index], **start) <= wall && wall < wall_of(self.zones[index + 1], **start))
                .map(|(index, start)| wall - Duration::seconds(offset_seconds(self.zones[index], *start)))
                .unwrap_or_else(|| wall - Duration::seconds(offset_seconds(self.zones[0], wall)))
        };
        utc.and_utc()
    }
}
