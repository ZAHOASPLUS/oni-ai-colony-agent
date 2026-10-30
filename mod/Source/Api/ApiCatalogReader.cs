using System;
using System.Collections.Generic;
using System.Linq;
using Newtonsoft.Json.Linq;

namespace OniAiProbeMod.Api
{
    /// <summary>What can be built, and what can be researched.</summary>
    internal static class ApiCatalogReader
    {
        private const int MaterialOptionsPerIngredient = 3;

        internal static object ReadBuildable(JObject args)
        {
            WorldContainer world = ApiValues.World(args);
            string category = ApiArgs.Text(args, "category");
            bool includeLocked = ApiArgs.Flag(args, "include_locked", true);

            List<string> categories = new List<string>();
            Dictionary<string, object> unlocked = new Dictionary<string, object>();
            Dictionary<string, object> locked = new Dictionary<string, object>();
            Dictionary<string, List<object>> optionsByTag = new Dictionary<string, List<object>>(StringComparer.Ordinal);
            foreach (PlanScreen.PlanInfo plan in TUNING.BUILDINGS.PLANORDER)
            {
                string planCategory = HashCache.Get().Get(plan.category);
                categories.Add(planCategory);
                if (category != null && !string.Equals(category, planCategory, StringComparison.OrdinalIgnoreCase))
                {
                    continue;
                }

                List<object> available = new List<object>();
                List<object> unavailable = new List<object>();
                foreach (KeyValuePair<string, string> item in plan.buildingAndSubcategoryData)
                {
                    BuildingDef def = Assets.GetBuildingDef(item.Key);
                    if (def == null || !def.IsAvailable() || !def.ShouldShowInBuildMenu() || !Game.IsCorrectDlcActiveForCurrentSave(def))
                    {
                        continue;
                    }
                    if (Db.Get().TechItems.IsTechItemComplete(def.PrefabID))
                    {
                        available.Add(DescribeBuilding(def, world, optionsByTag));
                    }
                    else
                    {
                        unavailable.Add(new Dictionary<string, object>
                        {
                            { "id", def.PrefabID },
                            { "tech", Db.Get().TechItems.GetTechFromItemID(def.PrefabID)?.Id }
                        });
                    }
                }
                if (available.Count > 0)
                {
                    unlocked[planCategory] = available;
                }
                if (includeLocked && unavailable.Count > 0)
                {
                    locked[planCategory] = unavailable;
                }
            }

            if (category != null && !categories.Contains(category, StringComparer.OrdinalIgnoreCase))
            {
                throw new ApiException(
                    "invalid_argument",
                    "No build category is called " + category + ".",
                    "Categories: " + string.Join(", ", categories));
            }
            return new Dictionary<string, object>
            {
                { "unlocked", unlocked },
                { "locked", locked }
            };
        }

        private static Dictionary<string, object> DescribeBuilding(
            BuildingDef def,
            WorldContainer world,
            Dictionary<string, List<object>> optionsByTag)
        {
            Dictionary<string, object> entry = new Dictionary<string, object>
            {
                { "id", def.PrefabID },
                { "name", STRINGS.UI.StripLinkFormatting(def.Name) },
                { "size", new[] { def.WidthInCells, def.HeightInCells } },
                { "rule", def.BuildLocationRule.ToString() },
                { "materials", DescribeMaterials(def, world, optionsByTag) }
            };
            if (def.PermittedRotations != PermittedRotations.Unrotatable)
            {
                entry["rotations"] = def.PermittedRotations.ToString();
            }
            if (def.isUtility)
            {
                entry["utility"] = true;
            }
            if (def.RequiresPowerInput)
            {
                entry["power_use_w"] = ApiValues.Number(def.EnergyConsumptionWhenActive, 0);
                entry["power_port"] = new[] { def.PowerInputOffset.x, def.PowerInputOffset.y };
            }
            if (def.RequiresPowerOutput)
            {
                entry["power_output_w"] = ApiValues.Number(def.GeneratorWattageRating, 0);
                entry["power_port"] = new[] { def.PowerOutputOffset.x, def.PowerOutputOffset.y };
            }
            if (def.InputConduitType != ConduitType.None)
            {
                entry["input"] = def.InputConduitType.ToString();
                entry["input_port"] = new[] { def.UtilityInputOffset.x, def.UtilityInputOffset.y };
            }
            if (def.OutputConduitType != ConduitType.None)
            {
                entry["output"] = def.OutputConduitType.ToString();
                entry["output_port"] = new[] { def.UtilityOutputOffset.x, def.UtilityOutputOffset.y };
            }
            return entry;
        }

        private static List<object> DescribeMaterials(
            BuildingDef def,
            WorldContainer world,
            Dictionary<string, List<object>> optionsByTag)
        {
            List<object> materials = new List<object>();
            if (def.CraftRecipe?.Ingredients == null)
            {
                return materials;
            }
            foreach (Recipe.Ingredient ingredient in def.CraftRecipe.Ingredients)
            {
                string tagName = ingredient.tag.Name;
                List<object> options;
                if (!optionsByTag.TryGetValue(tagName, out options))
                {
                    options = ReadMaterialOptions(ingredient.tag, world);
                    optionsByTag[tagName] = options;
                }
                materials.Add(new Dictionary<string, object>
                {
                    { "tag", tagName },
                    { "kg", ApiValues.Number(ingredient.amount, 0) },
                    { "best_available", options }
                });
            }
            return materials;
        }

        private static List<object> ReadMaterialOptions(Tag tag, WorldContainer world)
        {
            return MaterialSelector.GetValidMaterials(tag)
                .Select(element => new KeyValuePair<string, float>(element.Name, world.worldInventory.GetAmount(element, false)))
                .Where(option => option.Value > 0f)
                .OrderByDescending(option => option.Value)
                .Take(MaterialOptionsPerIngredient)
                .Select(option => (object)new Dictionary<string, object>
                {
                    { "element", option.Key },
                    { "kg", ApiValues.Number(option.Value, 0) }
                })
                .ToList();
        }

        // The game's lookup assumes a tech is being researched.
        internal static string MissingResearchBuilding()
        {
            if (Research.Instance.GetActiveResearch() == null)
            {
                return null;
            }
            string name = Research.Instance.GetMissingResearchBuildingName();
            return string.IsNullOrEmpty(name) ? null : STRINGS.UI.StripLinkFormatting(name);
        }

        internal static object ReadResearch(JObject args)
        {
            // "open" is what a player chooses from next; "all" is the whole tree.
            string scope = ApiArgs.Text(args, "scope") ?? "open";
            if (scope != "open" && scope != "all")
            {
                throw new ApiException("invalid_argument", "scope must be open or all.");
            }

            List<object> techs = new List<object>();
            List<object> complete = new List<object>();
            int locked = 0;
            foreach (Tech tech in Db.Get().Techs.resources)
            {
                TechInstance instance = Research.Instance.Get(tech);
                string state = tech.IsComplete() ? "complete" : tech.ArePrerequisitesComplete() ? "available" : "locked";
                if (state == "complete")
                {
                    complete.Add(tech.Id);
                }
                else if (state == "locked")
                {
                    locked++;
                }
                if (scope == "all" || state == "available")
                {
                    techs.Add(new Dictionary<string, object>
                    {
                        { "id", tech.Id },
                        { "name", STRINGS.UI.StripLinkFormatting(tech.Name) },
                        { "state", state },
                        { "tier", tech.tier },
                        { "cost", tech.costsByResearchTypeID },
                        { "progress", instance == null ? 0.0 : ApiValues.Number(instance.GetTotalPercentageComplete(), 3) },
                        { "requires", tech.requiredTech.Select(required => required.Id).ToList() },
                        { "unlocks", tech.unlockedItemIDs }
                    });
                }
            }

            TechInstance active = Research.Instance.GetActiveResearch();
            return new Dictionary<string, object>
            {
                { "current", active?.tech?.Id },
                { "queue", Research.Instance.GetResearchQueue().Select(item => item.tech.Id).ToList() },
                { "missing_research_building", MissingResearchBuilding() },
                { "complete", complete },
                { "locked_count", locked },
                { "techs", techs }
            };
        }
    }
}
