using System.Collections.Generic;
using System.Linq;
using Newtonsoft.Json.Linq;

namespace OniAiProbeMod.Api
{
    /// <summary>Skill points, and what each duplicant could spend them on.</summary>
    internal static class ApiSkills
    {
        internal static object Read(JObject args)
        {
            List<object> duplicants = new List<object>();
            foreach (MinionIdentity identity in Components.LiveMinionIdentities.Items)
            {
                if (identity == null)
                {
                    continue;
                }
                MinionResume resume = identity.GetComponent<MinionResume>();
                duplicants.Add(new Dictionary<string, object>
                {
                    { "name", identity.GetProperName() },
                    { "points", resume.AvailableSkillpoints },
                    {
                        "mastered",
                        resume.MasteryBySkillID.Where(entry => entry.Value).Select(entry => entry.Key).ToList()
                    },
                    { "granted", resume.GrantedSkillIDs.ToList() },
                    { "can_learn", Learnable(resume) }
                });
            }
            return new Dictionary<string, object> { { "duplicants", duplicants } };
        }

        internal static object Learn(JObject args)
        {
            MinionIdentity identity = ApiPlayTools.ReadDuplicant(args);
            string id = ApiArgs.Text(args, "skill");
            Database.Skill skill = Db.Get().Skills.TryGet(id);
            if (skill == null || skill.deprecated)
            {
                throw new ApiException("unknown_skill", "No skill is called " + id + ".", "get_skills lists what can be learned.");
            }
            MinionResume resume = identity.GetComponent<MinionResume>();
            MinionResume.SkillMasteryConditions[] conditions = resume.GetSkillMasteryConditions(id);
            if (resume.HasMasteredSkill(id) || !resume.CanMasterSkill(conditions))
            {
                throw new ApiException(
                    "cannot_learn",
                    identity.GetProperName() + " cannot learn " + id + " now: " + string.Join(", ", conditions.Select(condition => condition.ToString())),
                    "A skill needs a point and its prior skills, and a trait can rule it out.");
            }
            resume.MasterSkill(id);
            return new Dictionary<string, object>
            {
                { "duplicant", identity.GetProperName() },
                { "skill", id },
                { "points_left", resume.AvailableSkillpoints }
            };
        }

        private static List<object> Learnable(MinionResume resume)
        {
            if (resume.AvailableSkillpoints <= 0)
            {
                return new List<object>();
            }
            return Db.Get().Skills.resources
                .Where(skill => !skill.deprecated && !resume.HasMasteredSkill(skill.Id))
                .Where(skill => skill.requiredDuplicantModel == resume.GetComponent<MinionIdentity>().model.Name)
                .Where(skill => resume.CanMasterSkill(resume.GetSkillMasteryConditions(skill.Id)))
                .OrderBy(skill => skill.tier)
                .Select(skill => (object)new Dictionary<string, object>
                {
                    { "id", skill.Id },
                    { "group", skill.skillGroup },
                    { "interested", resume.HasSkillAptitude(skill) }
                })
                .ToList();
        }
    }
}
