using System;
using System.Collections.Generic;

namespace OniAiProbeMod.Probe
{
    /// <summary>
    /// Reads a deliberately small, versioned set of direct duplicant hazards for
    /// every live duplicant in the cluster, whichever world it is on.
    ///
    /// Code review 2026-09-26, G3: the reader used to scan only the camera's
    /// world, without child worlds, and still report the list as complete, so
    /// a duplicant suffocating on a second asteroid or inside a rocket produced
    /// alerts=[].  The flat list stays the canonical union (same shape);
    /// alerts_by_world tags each alert with the world it was observed on.
    ///
    /// This reader does not inspect NotificationManager/NotificationScreen state,
    /// localized titles, diagnostic messages, or private fields.  Every emitted ID
    /// is backed by a public typed monitor predicate configured on the corresponding
    /// duplicant model by the current game assembly.
    /// </summary>
    internal static class CanonicalAlertReader
    {
        internal const int DuplicantEnumerationLimit = 1000;
        internal const string Scope = "all_worlds";
        internal const string Source =
            "Components.LiveMinionIdentities.Items+GetMyWorldId+typed_monitor_predicates;catalog=" +
            CanonicalAlertCatalog.CatalogId;

        // Duplicants whose cell has no world (Grid.WorldIdx 255, e.g. between
        // cells in a transit tube) are still read; they are grouped here.
        private const int UnknownWorldKey = int.MinValue;

        internal static CanonicalAlertReadResult Read()
        {
            try
            {
                ClusterManager manager = ClusterManager.Instance;
                if (manager == null)
                {
                    return CanonicalAlertReadResult.Unsupported(Source, "cluster_manager_unavailable");
                }

                List<MinionIdentity> identities = Components.LiveMinionIdentities.Items;
                if (identities == null)
                {
                    return CanonicalAlertReadResult.Error(Source, "live_minion_registry_unavailable");
                }

                HashSet<int> instanceIds = new HashSet<int>();
                HashSet<string> alerts = new HashSet<string>(StringComparer.Ordinal);
                SortedDictionary<int, WorldAlerts> worlds = new SortedDictionary<int, WorldAlerts>();
                foreach (MinionIdentity identity in identities)
                {
                    if (instanceIds.Count >= DuplicantEnumerationLimit)
                    {
                        return CanonicalAlertReadResult.Error(Source, "duplicant_limit_exceeded");
                    }
                    if (identity == null)
                    {
                        return CanonicalAlertReadResult.Error(Source, "duplicant_identity_invalid");
                    }

                    KPrefabID prefab = identity.GetComponent<KPrefabID>();
                    if (prefab == null || prefab.InstanceID <= 0 || !instanceIds.Add(prefab.InstanceID))
                    {
                        return CanonicalAlertReadResult.Error(Source, "duplicant_identity_invalid");
                    }

                    bool standard = prefab.HasTag(GameTags.Minions.Models.Standard);
                    bool bionic = prefab.HasTag(GameTags.Minions.Models.Bionic);
                    if (standard == bionic)
                    {
                        return CanonicalAlertReadResult.Error(Source, "duplicant_model_invalid");
                    }

                    int worldId = identity.GetMyWorldId();
                    int worldKey = worldId < 0 ? UnknownWorldKey : worldId;
                    WorldAlerts world;
                    if (!worlds.TryGetValue(worldKey, out world))
                    {
                        world = new WorldAlerts();
                        worlds.Add(worldKey, world);
                    }
                    world.DuplicantCount++;

                    string failureReason;
                    if (!ReadCommonHazards(identity, world.Alerts, out failureReason) ||
                        (standard
                            ? !ReadStandardHazards(identity, world.Alerts, out failureReason)
                            : !ReadBionicHazards(identity, world.Alerts, out failureReason)))
                    {
                        return CanonicalAlertReadResult.Error(Source, failureReason);
                    }
                }

                foreach (WorldAlerts world in worlds.Values)
                {
                    alerts.UnionWith(world.Alerts);
                }
                CanonicalAlertReadResult union = CanonicalAlertCatalog.Observed(alerts, Source);
                if (union.Status != "observed")
                {
                    return union;
                }

                List<Dictionary<string, object>> tagged = new List<Dictionary<string, object>>(worlds.Count);
                foreach (KeyValuePair<int, WorldAlerts> entry in worlds)
                {
                    // A subset of an accepted union: canonicalization cannot fail.
                    CanonicalAlertReadResult world = CanonicalAlertCatalog.Observed(entry.Value.Alerts, Source);
                    tagged.Add(new Dictionary<string, object>
                    {
                        { "world_id", entry.Key == UnknownWorldKey ? null : (object)entry.Key },
                        { "duplicant_count", entry.Value.DuplicantCount },
                        { "alerts", world.Alerts }
                    });
                }
                return union.WithWorlds(tagged);
            }
            catch
            {
                return CanonicalAlertReadResult.Error(Source, "typed_alert_read_failed");
            }
        }

        private sealed class WorldAlerts
        {
            internal readonly HashSet<string> Alerts = new HashSet<string>(StringComparer.Ordinal);
            internal int DuplicantCount;
        }

        private static bool ReadCommonHazards(
            MinionIdentity identity,
            ISet<string> alerts,
            out string failureReason)
        {
            SuffocationMonitor.Instance suffocation = identity.GetSMI<SuffocationMonitor.Instance>();
            StressMonitor.Instance stress = identity.GetSMI<StressMonitor.Instance>();
            SicknessMonitor.Instance sickness = identity.GetSMI<SicknessMonitor.Instance>();
            TemperatureMonitor.Instance temperature = identity.GetSMI<TemperatureMonitor.Instance>();
            Health health = identity.GetComponent<Health>();
            if (suffocation == null || suffocation.oxygenBreather == null || stress == null ||
                sickness == null || temperature == null || health == null)
            {
                failureReason = "common_monitor_missing";
                return false;
            }

            // low_oxygen is a direct, per-duplicant breathing hazard only: the
            // current breather cannot breathe or reports low carried/ambient O2.
            // It is never inferred from inventory, localized text, or UI alerts.
            if (!suffocation.CanBreath() || suffocation.oxygenBreather.IsLowOxygen())
            {
                alerts.Add(CanonicalAlertCatalog.LowOxygen);
            }
            if (suffocation.IsSuffocating())
            {
                alerts.Add(CanonicalAlertCatalog.Suffocation);
            }
            if (stress.IsStressed())
            {
                alerts.Add(CanonicalAlertCatalog.HighStress);
            }
            if (sickness.IsSick())
            {
                alerts.Add(CanonicalAlertCatalog.Sickness);
            }
            if (sickness.HasMajorDisease())
            {
                alerts.Add(CanonicalAlertCatalog.MajorSickness);
            }
            if (temperature.IsHyperthermic())
            {
                alerts.Add(CanonicalAlertCatalog.Hyperthermia);
            }
            if (temperature.IsHypothermic())
            {
                alerts.Add(CanonicalAlertCatalog.Hypothermia);
            }

            switch (health.State)
            {
                case Health.HealthState.Scuffed:
                case Health.HealthState.Injured:
                case Health.HealthState.Critical:
                    alerts.Add(CanonicalAlertCatalog.Wounded);
                    break;
                case Health.HealthState.Incapacitated:
                    alerts.Add(CanonicalAlertCatalog.Incapacitated);
                    break;
                case Health.HealthState.Dead:
                    failureReason = "live_minion_health_state_invalid";
                    return false;
            }

            failureReason = null;
            return true;
        }

        private static bool ReadStandardHazards(
            MinionIdentity identity,
            ISet<string> alerts,
            out string failureReason)
        {
            CalorieMonitor.Instance calories = identity.GetSMI<CalorieMonitor.Instance>();
            BladderMonitor.Instance bladder = identity.GetSMI<BladderMonitor.Instance>();
            StaminaMonitor.Instance stamina = identity.GetSMI<StaminaMonitor.Instance>();
            if (calories == null || bladder == null || stamina == null)
            {
                failureReason = "standard_monitor_missing";
                return false;
            }

            if (calories.IsStarving())
            {
                alerts.Add(CanonicalAlertCatalog.Starvation);
            }
            if (bladder.NeedsToPee())
            {
                alerts.Add(CanonicalAlertCatalog.UrgentToilet);
            }
            if (stamina.NeedsToSleep())
            {
                alerts.Add(CanonicalAlertCatalog.Exhaustion);
            }

            failureReason = null;
            return true;
        }

        private static bool ReadBionicHazards(
            MinionIdentity identity,
            ISet<string> alerts,
            out string failureReason)
        {
            BionicOxygenTankMonitor.Instance oxygenTank =
                identity.GetSMI<BionicOxygenTankMonitor.Instance>();
            BionicBatteryMonitor.Instance battery = identity.GetSMI<BionicBatteryMonitor.Instance>();
            if (oxygenTank == null || battery == null)
            {
                failureReason = "bionic_monitor_missing";
                return false;
            }

            float oxygenPercentage = oxygenTank.OxygenPercentage;
            if (float.IsNaN(oxygenPercentage) || float.IsInfinity(oxygenPercentage) ||
                oxygenPercentage < 0.0f || oxygenPercentage > 1.0f)
            {
                failureReason = "bionic_oxygen_invalid";
                return false;
            }
            // The 50% threshold is the typed threshold used by the game's own
            // BreathabilityDiagnostic for a low bionic oxygen tank.
            if (oxygenPercentage < 0.5f)
            {
                alerts.Add(CanonicalAlertCatalog.LowOxygen);
            }
            if (BionicBatteryMonitor.IsCriticallyLow(battery))
            {
                alerts.Add(CanonicalAlertCatalog.BionicLowBattery);
            }
            if (!battery.IsOnline)
            {
                alerts.Add(CanonicalAlertCatalog.BionicOffline);
            }

            failureReason = null;
            return true;
        }
    }
}
