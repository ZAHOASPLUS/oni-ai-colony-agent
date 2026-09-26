using System;
using System.Collections;
using System.Collections.Generic;
using System.Linq;
using OniAiProbeMod.Probe;

namespace OniAiProbeMod.Probe
{
    internal static class Program
    {
        private const string Source = "harness";
        private static readonly List<string> Passed = new List<string>();

        private static int Main()
        {
            try
            {
                CatalogNormalizesAndDeduplicates();
                CatalogRejectsUnknownAndOversizedInputs();
                StandardHazardsAreTypedAndSorted();
                BionicHazardsAreTypedAndSorted();
                ReaderFailsClosed();
                ReaderCoversEveryWorld();
                Console.WriteLine(
                    "PASS canonical alerts harness: " +
                    string.Join(", ", Passed) +
                    $"; catalog={CanonicalAlertCatalog.CatalogId}; alerts={CanonicalAlertCatalog.AllKnownIds.Count}");
                return 0;
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine("FAIL canonical alerts harness: " + ex.Message);
                return 1;
            }
        }

        private static void CatalogNormalizesAndDeduplicates()
        {
            CanonicalAlertReadResult result = CanonicalAlertCatalog.Observed(
                new[]
                {
                    CanonicalAlertCatalog.Starvation,
                    CanonicalAlertCatalog.LowOxygen,
                    CanonicalAlertCatalog.Starvation
                },
                Source);
            Require(result.Status == "observed" && result.Completeness == "complete", "catalog result not observed/complete");
            Require(
                result.Alerts.SequenceEqual(new[] { CanonicalAlertCatalog.LowOxygen, CanonicalAlertCatalog.Starvation }),
                "catalog did not ordinal-sort and deduplicate");
            Require(CanonicalAlertCatalog.AllKnownIds.Count <= CanonicalAlertCatalog.MaximumAlertCount, "catalog exceeds 128 IDs");
            Require(CanonicalAlertCatalog.IsKnown(CanonicalAlertCatalog.LowOxygen), "known ID lookup failed");
            Require(!CanonicalAlertCatalog.IsKnown("unknown_hazard"), "unknown ID lookup passed");
            Passed.Add("catalog_sort_dedupe");
        }

        private static void CatalogRejectsUnknownAndOversizedInputs()
        {
            CanonicalAlertReadResult unknown = CanonicalAlertCatalog.Observed(new[] { "unknown_hazard" }, Source);
            Require(unknown.Status == "error" && unknown.Value == null && unknown.FailureReason == "unknown_alert_id", "unknown ID did not fail closed");

            List<string> oversized = Enumerable.Range(0, CanonicalAlertCatalog.MaximumAlertCount + 1)
                .Select(index => "alert_" + index.ToString("000"))
                .ToList();
            CanonicalAlertReadResult tooMany = CanonicalAlertCatalog.Observed(oversized, Source);
            Require(tooMany.Status == "error" && tooMany.Value == null && tooMany.FailureReason == "alert_limit_exceeded", "129 IDs did not fail closed");
            Passed.Add("catalog_fail_closed");
        }

        private static void StandardHazardsAreTypedAndSorted()
        {
            ResetWorld(7);
            MinionIdentity standard = StandardMinion(101);
            standard.Smi(new SuffocationMonitor.Instance
            {
                CanBreathe = false,
                Suffocating = true,
                oxygenBreather = new OxygenBreather { LowOxygen = true }
            });
            standard.Smi(new StressMonitor.Instance { Stressed = true });
            standard.Smi(new SicknessMonitor.Instance { Sick = true, MajorDisease = true });
            standard.Smi(new TemperatureMonitor.Instance { Hyperthermic = true, Hypothermic = true });
            standard.Component(new Health { State = Health.HealthState.Critical });
            standard.Smi(new CalorieMonitor.Instance { Starving = true });
            standard.Smi(new BladderMonitor.Instance { Urgent = true });
            standard.Smi(new StaminaMonitor.Instance { Exhausted = true });
            Components.LiveMinionIdentities.Items.Add(standard);

            CanonicalAlertReadResult result = CanonicalAlertReader.Read();
            Require(result.Status == "observed" && result.Alerts.SequenceEqual(result.Alerts.OrderBy(item => item, StringComparer.Ordinal)), "standard alerts not observed/sorted");
            Require(result.Source == CanonicalAlertReader.Source, "reader source did not match canonical source");
            Require(ReferenceEquals(result.Value, result.Alerts), "observed value did not expose the canonical list");
            Require(result.Alerts.Contains(CanonicalAlertCatalog.LowOxygen), "typed low_oxygen missing");
            Require(result.Alerts.Contains(CanonicalAlertCatalog.Suffocation), "typed suffocation missing");
            Require(result.Alerts.Contains(CanonicalAlertCatalog.Starvation), "typed starvation missing");
            Require(result.Alerts.Contains(CanonicalAlertCatalog.UrgentToilet), "typed urgent_toilet missing");
            Require(result.Alerts.Contains(CanonicalAlertCatalog.Exhaustion), "typed exhaustion missing");
            Require(result.Alerts.Contains(CanonicalAlertCatalog.HighStress), "typed high_stress missing");
            Require(result.Alerts.Contains(CanonicalAlertCatalog.MajorSickness), "typed major_sickness missing");
            Require(result.Alerts.Contains(CanonicalAlertCatalog.Hyperthermia), "typed hyperthermia missing");
            Require(result.Alerts.Contains(CanonicalAlertCatalog.Hypothermia), "typed hypothermia missing");
            Require(result.Alerts.Contains(CanonicalAlertCatalog.Wounded), "typed wounded missing");
            Passed.Add("standard_typed_predicates");
        }

        private static void BionicHazardsAreTypedAndSorted()
        {
            ResetWorld(8);
            MinionIdentity bionic = BionicMinion(202);
            bionic.Smi(new SuffocationMonitor.Instance
            {
                CanBreathe = true,
                Suffocating = false,
                oxygenBreather = new OxygenBreather { LowOxygen = false }
            });
            bionic.Smi(new StressMonitor.Instance());
            bionic.Smi(new SicknessMonitor.Instance());
            bionic.Smi(new TemperatureMonitor.Instance());
            bionic.Component(new Health { State = Health.HealthState.Incapacitated });
            bionic.Smi(new BionicOxygenTankMonitor.Instance { OxygenPercentage = 0.49f });
            bionic.Smi(new BionicBatteryMonitor.Instance { CriticallyLow = true, IsOnline = false });
            Components.LiveMinionIdentities.Items.Add(bionic);

            CanonicalAlertReadResult result = CanonicalAlertReader.Read();
            Require(result.Status == "observed", "bionic alerts not observed");
            Require(result.Alerts.Contains(CanonicalAlertCatalog.LowOxygen), "bionic low oxygen missing");
            Require(result.Alerts.Contains(CanonicalAlertCatalog.BionicLowBattery), "bionic low battery missing");
            Require(result.Alerts.Contains(CanonicalAlertCatalog.BionicOffline), "bionic offline missing");
            Require(result.Alerts.Contains(CanonicalAlertCatalog.Incapacitated), "bionic incapacitation missing");

            ResetWorld(8);
            MinionIdentity invalidOxygen = BionicMinion(203);
            invalidOxygen.Smi(new SuffocationMonitor.Instance
            {
                CanBreathe = true,
                oxygenBreather = new OxygenBreather()
            });
            invalidOxygen.Smi(new StressMonitor.Instance());
            invalidOxygen.Smi(new SicknessMonitor.Instance());
            invalidOxygen.Smi(new TemperatureMonitor.Instance());
            invalidOxygen.Component(new Health { State = Health.HealthState.Perfect });
            invalidOxygen.Smi(new BionicOxygenTankMonitor.Instance { OxygenPercentage = 1.01f });
            invalidOxygen.Smi(new BionicBatteryMonitor.Instance());
            Components.LiveMinionIdentities.Items.Add(invalidOxygen);
            CanonicalAlertReadResult invalid = CanonicalAlertReader.Read();
            Require(invalid.Status == "error" && invalid.Value == null && invalid.FailureReason == "bionic_oxygen_invalid", "out-of-range bionic oxygen did not fail closed");
            Passed.Add("bionic_typed_predicates");
        }

        private static void ReaderFailsClosed()
        {
            ClusterManager.Instance = null;
            CanonicalAlertReadResult unsupported = CanonicalAlertReader.Read();
            Require(
                unsupported.Status == "unsupported" &&
                unsupported.Value == null &&
                unsupported.Completeness == "unknown" &&
                unsupported.Source == CanonicalAlertReader.Source,
                "unavailable game context did not publish a canonical unsupported result");

            ResetWorld(9);
            MinionIdentity missing = StandardMinion(303);
            Components.LiveMinionIdentities.Items.Add(missing);
            CanonicalAlertReadResult missingMonitor = CanonicalAlertReader.Read();
            Require(missingMonitor.Status == "error" && missingMonitor.Value == null, "missing SMI produced a partial list");

            ResetWorld(9);
            Components.LiveMinionIdentities.Items = new List<MinionIdentity> { null };
            CanonicalAlertReadResult destroyed = CanonicalAlertReader.Read();
            Require(destroyed.Status == "error" && destroyed.Value == null && destroyed.WorldsValue == null && destroyed.FailureReason == "duplicant_identity_invalid", "null/destroyed identity did not fail closed");

            ResetWorld(9);
            MinionIdentity first = HealthyStandardMinion(404);
            Components.LiveMinionIdentities.Items.Add(first);
            Components.LiveMinionIdentities.Items.Add(first);
            CanonicalAlertReadResult duplicate = CanonicalAlertReader.Read();
            Require(duplicate.Status == "error" && duplicate.FailureReason == "duplicant_identity_invalid", "duplicate identity did not fail closed");

            ResetWorld(9);
            for (int index = 0; index <= CanonicalAlertReader.DuplicantEnumerationLimit; index++)
            {
                Components.LiveMinionIdentities.Items.Add(HealthyStandardMinion(index + 1));
            }
            CanonicalAlertReadResult oversized = CanonicalAlertReader.Read();
            Require(oversized.Status == "error" && oversized.FailureReason == "duplicant_limit_exceeded", "duplicant cap did not fail closed");
            Passed.Add("reader_fail_closed");
        }

        // Code review 2026-09-26, G3: a duplicant suffocating on a second
        // asteroid or in a rocket interior must reach the canonical list, and
        // every alert must carry the world it was observed on.
        private static void ReaderCoversEveryWorld()
        {
            ResetWorld(0);
            MinionIdentity home = HealthyStandardMinion(601);
            MinionIdentity rocket = HealthyStandardMinion(602);
            rocket.WorldId = 2;
            rocket.Smi(new SuffocationMonitor.Instance
            {
                CanBreathe = false,
                Suffocating = true,
                oxygenBreather = new OxygenBreather()
            });
            MinionIdentity inTransit = HealthyStandardMinion(603);
            inTransit.WorldId = -1;
            inTransit.Smi(new CalorieMonitor.Instance { Starving = true });
            Components.LiveMinionIdentities.Items.AddRange(new[] { rocket, home, inTransit });

            CanonicalAlertReadResult result = CanonicalAlertReader.Read();
            Require(result.Status == "observed" && result.Completeness == "complete", "multi-world read not observed/complete");
            Require(
                result.Alerts.SequenceEqual(new[]
                {
                    CanonicalAlertCatalog.LowOxygen,
                    CanonicalAlertCatalog.Starvation,
                    CanonicalAlertCatalog.Suffocation
                }),
                "off-camera hazards missing from the canonical union: " + string.Join(",", result.Alerts));
            Require(ReferenceEquals(result.WorldsValue, result.Worlds) && result.Worlds.Count == 3, "world groups missing");
            RequireWorld(result.Worlds[0], null, 1, CanonicalAlertCatalog.Starvation);
            RequireWorld(result.Worlds[1], 0, 1);
            RequireWorld(result.Worlds[2], 2, 1, CanonicalAlertCatalog.LowOxygen, CanonicalAlertCatalog.Suffocation);

            // The camera's world is irrelevant to a colony-wide read.
            ClusterManager.Instance.activeWorldId = -1;
            CanonicalAlertReadResult noCamera = CanonicalAlertReader.Read();
            Require(noCamera.Status == "observed" && noCamera.Alerts.SequenceEqual(result.Alerts), "read depended on the active world");

            ResetWorld(0);
            CanonicalAlertReadResult empty = CanonicalAlertReader.Read();
            Require(empty.Status == "observed" && empty.Alerts.Count == 0 && empty.Worlds.Count == 0, "empty colony not observed as empty");
            Passed.Add("all_worlds_tagged");
        }

        private static void RequireWorld(Dictionary<string, object> world, int? worldId, int duplicants, params string[] alerts)
        {
            object id = world["world_id"];
            Require(worldId.HasValue ? id is int value && value == worldId.Value : id == null, "world id tag is wrong");
            Require((int)world["duplicant_count"] == duplicants, "world duplicant count is wrong");
            Require(((List<string>)world["alerts"]).SequenceEqual(alerts), "world alerts are wrong for world " + (id ?? "null"));
            Require(world.Count == 3, "world group has unexpected fields");
        }

        private static void ResetWorld(int worldId)
        {
            ClusterManager.Instance = new ClusterManager { activeWorldId = worldId };
            Components.LiveMinionIdentities = new LiveMinionRegistry();
        }

        private static MinionIdentity StandardMinion(int instanceId)
        {
            MinionIdentity identity = new MinionIdentity { WorldId = ClusterManager.Instance.activeWorldId };
            identity.Component(new KPrefabID(instanceId, GameTags.Minions.Models.Standard));
            return identity;
        }

        private static MinionIdentity BionicMinion(int instanceId)
        {
            MinionIdentity identity = new MinionIdentity { WorldId = ClusterManager.Instance.activeWorldId };
            identity.Component(new KPrefabID(instanceId, GameTags.Minions.Models.Bionic));
            return identity;
        }

        private static MinionIdentity HealthyStandardMinion(int instanceId)
        {
            MinionIdentity identity = StandardMinion(instanceId);
            identity.Smi(new SuffocationMonitor.Instance { CanBreathe = true, oxygenBreather = new OxygenBreather() });
            identity.Smi(new StressMonitor.Instance());
            identity.Smi(new SicknessMonitor.Instance());
            identity.Smi(new TemperatureMonitor.Instance());
            identity.Component(new Health { State = Health.HealthState.Perfect });
            identity.Smi(new CalorieMonitor.Instance());
            identity.Smi(new BladderMonitor.Instance());
            identity.Smi(new StaminaMonitor.Instance());
            return identity;
        }

        private static void Require(bool condition, string message)
        {
            if (!condition)
            {
                throw new InvalidOperationException(message);
            }
        }
    }
}

// Minimal typed API doubles.  The harness links the production reader source,
// so compile-time API drift and all fail-closed branches are exercised without
// loading or controlling ONI.
internal sealed class ClusterManager
{
    internal static ClusterManager Instance { get; set; }
    internal int activeWorldId;
}

internal static class Components
{
    internal static LiveMinionRegistry LiveMinionIdentities = new LiveMinionRegistry();
}

internal sealed class LiveMinionRegistry
{
    internal List<MinionIdentity> Items { get; set; } = new List<MinionIdentity>();
}

internal sealed class MinionIdentity
{
    private readonly Dictionary<Type, object> components = new Dictionary<Type, object>();
    private readonly Dictionary<Type, object> smis = new Dictionary<Type, object>();

    internal int WorldId { get; set; }

    internal int GetMyWorldId() => WorldId;

    internal T GetComponent<T>() where T : class
    {
        return components.TryGetValue(typeof(T), out object value) ? (T)value : null;
    }

    internal T GetSMI<T>() where T : class
    {
        return smis.TryGetValue(typeof(T), out object value) ? (T)value : null;
    }

    internal void Component<T>(T value) where T : class => components[typeof(T)] = value;

    internal void Smi<T>(T value) where T : class => smis[typeof(T)] = value;
}

internal sealed class Tag
{
    internal Tag(string id) { Id = id; }
    internal string Id { get; }
}

internal static class GameTags
{
    internal static class Minions
    {
        internal static class Models
        {
            internal static readonly Tag Standard = new Tag("standard");
            internal static readonly Tag Bionic = new Tag("bionic");
        }
    }
}

internal sealed class KPrefabID
{
    private readonly HashSet<Tag> tags = new HashSet<Tag>();
    internal KPrefabID(int instanceId, Tag tag) { InstanceID = instanceId; tags.Add(tag); }
    internal int InstanceID { get; }
    internal bool HasTag(Tag tag) => tags.Contains(tag);
}

internal sealed class OxygenBreather
{
    internal bool LowOxygen { get; set; }
    internal bool IsLowOxygen() => LowOxygen;
}

internal sealed class SuffocationMonitor
{
    internal sealed class Instance
    {
        internal OxygenBreather oxygenBreather { get; set; }
        internal bool CanBreathe { get; set; }
        internal bool Suffocating { get; set; }
        internal bool CanBreath() => CanBreathe;
        internal bool IsSuffocating() => Suffocating;
    }
}

internal sealed class StressMonitor
{
    internal sealed class Instance { internal bool Stressed { get; set; } internal bool IsStressed() => Stressed; }
}

internal sealed class SicknessMonitor
{
    internal sealed class Instance
    {
        internal bool Sick { get; set; }
        internal bool MajorDisease { get; set; }
        internal bool IsSick() => Sick;
        internal bool HasMajorDisease() => MajorDisease;
    }
}

internal sealed class TemperatureMonitor
{
    internal sealed class Instance
    {
        internal bool Hyperthermic { get; set; }
        internal bool Hypothermic { get; set; }
        internal bool IsHyperthermic() => Hyperthermic;
        internal bool IsHypothermic() => Hypothermic;
    }
}

internal sealed class Health
{
    internal enum HealthState { Perfect, Alright, Scuffed, Injured, Critical, Incapacitated, Dead, Invincible }
    internal HealthState State { get; set; }
}

internal sealed class CalorieMonitor
{
    internal sealed class Instance { internal bool Starving { get; set; } internal bool IsStarving() => Starving; }
}

internal sealed class BladderMonitor
{
    internal sealed class Instance { internal bool Urgent { get; set; } internal bool NeedsToPee() => Urgent; }
}

internal sealed class StaminaMonitor
{
    internal sealed class Instance { internal bool Exhausted { get; set; } internal bool NeedsToSleep() => Exhausted; }
}

internal sealed class BionicOxygenTankMonitor
{
    internal sealed class Instance { internal float OxygenPercentage { get; set; } }
}

internal sealed class BionicBatteryMonitor
{
    internal sealed class Instance
    {
        internal bool CriticallyLow { get; set; }
        internal bool IsOnline { get; set; } = true;
    }

    internal static bool IsCriticallyLow(Instance instance) => instance.CriticallyLow;
}
