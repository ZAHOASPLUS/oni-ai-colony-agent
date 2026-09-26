using System;
using System.Collections.Generic;

namespace OniAiProbeMod.Probe
{
    /// <summary>
    /// Reads a deliberately small, versioned set of direct active-world hazards.
    ///
    /// This reader does not inspect NotificationManager/NotificationScreen state,
    /// localized titles, diagnostic messages, or private fields.  Every emitted ID
    /// is backed by a public typed monitor predicate configured on the corresponding
    /// duplicant model by the current game assembly.
    /// </summary>
    internal static class CanonicalAlertReader
    {
        internal const int DuplicantEnumerationLimit = 1000;
        internal const string Source =
            "Components.LiveMinionIdentities.GetWorldItems(activeWorldId,false)+typed_monitor_predicates;catalog=" +
            CanonicalAlertCatalog.CatalogId;

        internal static CanonicalAlertReadResult Read()
        {
            try
            {
                ClusterManager manager = ClusterManager.Instance;
                if (manager == null)
                {
                    return CanonicalAlertReadResult.Unsupported(Source, "cluster_manager_unavailable");
                }

                int activeWorldId = manager.activeWorldId;
                if (activeWorldId < 0)
                {
                    return CanonicalAlertReadResult.Unsupported(Source, "active_world_unavailable");
                }

                IEnumerable<MinionIdentity> identities = Components.LiveMinionIdentities.GetWorldItems(
                    activeWorldId,
                    checkChildWorlds: false);
                if (identities == null)
                {
                    return CanonicalAlertReadResult.Error(Source, "live_minion_registry_unavailable");
                }

                HashSet<int> instanceIds = new HashSet<int>();
                HashSet<string> alerts = new HashSet<string>(StringComparer.Ordinal);
                foreach (MinionIdentity identity in identities)
                {
                    if (instanceIds.Count >= DuplicantEnumerationLimit)
                    {
                        return CanonicalAlertReadResult.Error(Source, "duplicant_limit_exceeded");
                    }
                    if (identity == null || identity.GetMyWorldId() != activeWorldId)
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

                    string failureReason;
                    if (!ReadCommonHazards(identity, alerts, out failureReason) ||
                        (standard
                            ? !ReadStandardHazards(identity, alerts, out failureReason)
                            : !ReadBionicHazards(identity, alerts, out failureReason)))
                    {
                        return CanonicalAlertReadResult.Error(Source, failureReason);
                    }
                }

                // Bind the complete scan to one active world.  A UI world switch
                // during enumeration makes the whole field unusable rather than
                // returning a mixed-world empty/partial list.
                if (!ReferenceEquals(ClusterManager.Instance, manager) || manager.activeWorldId != activeWorldId)
                {
                    return CanonicalAlertReadResult.Error(Source, "active_world_changed_during_read");
                }

                return CanonicalAlertCatalog.Observed(alerts, Source);
            }
            catch
            {
                return CanonicalAlertReadResult.Error(Source, "typed_alert_read_failed");
            }
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
