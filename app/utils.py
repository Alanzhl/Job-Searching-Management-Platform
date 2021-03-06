from passlib.hash import bcrypt_sha256
from datetime import date
from elasticsearch import client

from app.models import Users, JobPost, MyError
from .config import Config


# used for name and email checks in a registration process
def checkExistence(name, email):
    exist_name = Users.query.filter_by(name=name).first()
    exist_email = Users.query.filter_by(email=email).first()
    errors = []
    if exist_name:
        errors.append("Your user name has been used. Pls use a new name.")
    if exist_email:
        errors.append("Your email has been registered. Pls try a new one.")
    return errors


# used for name and password checks when trying to login in with username
def checkByName(name, password):
    user = Users.query.filter_by(name=name).first()
    errors = []
    if user is None:
        errors.append("No such user exists, pls check your inputs.")
    elif not bcrypt_sha256.verify(str(password), user.password):
        errors.append("Password does not match the given user. Pls try again.")
    return errors


# used for email and password checks when trying to login in with email
def checkByEmail(email, password):
    user = Users.query.filter_by(email=email).first()
    errors = []
    if user is None:
        errors.append("No user with this email exists, pls check your inputs.")
    elif not bcrypt_sha256.verify(str(password), user.password):
        errors.append("Password does not match the given user. Pls try again.")
    return errors


# check if the job post with post_id "id" already exists in ES
def checkESPost(es, id):
    query = {
        "query": {
            "match_phrase": {
                "post_id": id
            }
        }
    }
    return es.search(index="index_jobposts", body=query)["hits"]["total"]["value"] > 0


# get the corresponding job post records from MySQL with the searching result
def get_post_MySQL(response):
    idx = 0
    id_dict = {}    # order of the results sorted by the scores
    for record in response:
        id_dict[record["_source"]["post_id"]] = idx
        idx += 1
    search_results = JobPost.query.filter(JobPost.post_id.in_(id_dict.keys()))
    displays = [None] * idx
    for record in search_results:
        displays[id_dict[record.post_id]] = create_post(record)    # reorder the search results as how they were returned from ES
    return displays


# turn a MySQL record object into a displayable post
def create_post(record):
    post = {}
    post["post_id"] = record.post_id
    post["title"] = record.title
    post["link"] = record.link if record.link else None
    post["company"] = record.company
    post["salary_min"] = record.salary_min if record.salary_min > 0 else record.salary_max
    post["salary_max"] = record.salary_max if record.salary_max > 0 else record.salary_min
    if post["salary_min"] == 0 and post["salary_max"] == 0:
        post["salary"] = "not given"
    else:
        post["salary"] = " - ".join(["$"+str(post["salary_min"]), "$"+str(post["salary_max"])])
    post["date"] = record.date
    post["description"] = record.description

    return post


# generate a job post in ES-required format
def genESPost(post, id):
    esPost = {}
    esPost["_index"] = "index_jobposts"
    esPost["post_id"] = id
    esPost["title"] = post["title"]
    esPost["company"] = post["company"]
    esPost["description"] = post["description"]
    return esPost


# turn a MySQL user object into a displayable form
def create_userinfo(raw_user):
    user = {}
    
    user["user_id"] = raw_user.user_id
    user["name"] = raw_user.name
    user["email"] = raw_user.email
    if raw_user.role_id == 2:
        user["role"] = "Company"
    elif raw_user.role_id == 3:
        user["role"] = "Administrator"
    else:
        user["role"] = "Job Seeker"
    
    return user


# filter the search keyword so that it only contains characters legal for a cookie
def filter_keyword(kw):
    legal_words = Config.PATTERN.findall(kw)
    return " ".join(legal_words)


# non-destructive filter from a list of displayable results (format shown in function "create_post")
def filter_results(results, kw, val):
    filtered_results = []
    try:
        if val == "all":
            filtered_results = results
        elif kw == "date":
            val_num = int(val)
            if val_num == 1000:
                filtered_results = results
            else:
                today = date.today()
                for post in results:
                    delta = today - post["date"]
                    if delta.days <= val_num:
                        filtered_results.append(post)
        elif kw == "salary":
            if val == "None":
                for post in results:
                    if post["salary"] == "not given": 
                        filtered_results.append(post)
            else:
                category, thres_str = val.split("_")
                thres = int(thres_str)
                if category == "min":
                    for post in results:
                        if post["salary_min"] >= thres:
                            filtered_results.append(post)
                else:
                    for post in results:
                        if post["salary_max"] >= thres:
                            filtered_results.append(post)
        # other cases: filter by exact values (categorical data)
        else:
            if kw == "role":
                if val == "admin":
                    val = "Administrator"
                elif val == "company":
                    val = "Company"
                else:
                    val = "Job Seeker"
            for user in results:
                if user[kw] == val:
                    filtered_results.append(user)
    except Exception as e:
        MyError.display("JobFilter Error", MyError.UI_REQUEST_UNKNOWN, "unknown keyword sent by UI.")
        print(e)
        return results

    return filtered_results


# destructively sort a list of displayable results
def sort_results(results, kw, val):
    try:
        if kw == "salary":
            if val == "desc_min":
                sort_helper(results, lambda x : x["salary_min"])
            elif val == "desc_max":
                sort_helper(results, lambda x : x["salary_max"])
            elif val == "asc_min":
                sort_helper(results, lambda x : x["salary_min"], asc=True)
            else: 
                sort_helper(results, lambda x : x["salary_max"], asc=True)
        else:
            field = kw
            if field == "userid":
                field = "user_id"
            elif field == "postid":
                field = "post_id"
            
            if val == "asc":
                sort_helper(results, lambda x : x[field], asc=True)
            else:
                sort_helper(results, lambda x : x[field])
    except Exception as e:
        MyError.display("JobFilter Error", MyError.UI_REQUEST_UNKNOWN, "unknown keyword sent by UI.")
        print(e)
    
    return results


# help sort the results according to keyword "key". Mergesort is applied to enable multi-keyword-sorting.
def sort_helper(posts, key, asc=False):
    length = len(posts)
    
    mergesort(posts, key, 0, length - 1)
    if asc:
        for i in range(length >> 1):
            posts[i], posts[length - 1 - i] = posts[length - 1 - i], posts[i]


# Notice: this is a destructive sorting!
def mergesort(lst, key, start, end):
    if start >= end: return

    mid = (start + end) >> 1
    mergesort(lst, key, start, mid)
    mergesort(lst, key, mid + 1, end)
    merge(lst, key, start, mid + 1, end)


def merge(lst, key, start, mid, end):
    aux = [lst[i] for i in range(start, mid)]    # save half of the space consumed
    i, j, k = 0, mid, start

    while i < mid - start and j <= end:
        if key(aux[i]) >= key(lst[j]):
            lst[k] = aux[i]
            i += 1
        else:
            lst[k] = lst[j]
            j += 1
        k += 1
    
    while i < mid - start:
        lst[k] = aux[i]
        i += 1
        k += 1


# split a search keyword with the default analyzer adopted by "index_jobposts"
def split_keyword(es, kw):
    body = {
        "text": kw
    }
    es_client = client.IndicesClient(es)
    response = es_client.analyze(index="index_jobposts", body=body)
    words = []
    for item in response["tokens"]:
        words.append(item["token"])
    return words
    

# transfer a search history from string to python dict
def transfer_history_2dict(history_str):
    history_dict = {}
    if history_str:
        # neighboring keyword and frequency pairs are splitted by "&"
        items = history_str.split("&")
        for item in items:
            if item != "":
                # key and value are splitted by "+"
                key, val = item.split("+")
                history_dict[key] = int(val)
    return history_dict


# transfer a search history from python dict to a string (for storage in )
def transfer_history_2str(history_dict):
    word_list = []
    if history_dict:
        for key, val in history_dict.items():
            word_list.append("+".join([key, str(val)]))
    return "&".join(word_list)


# combine the 3 most frequently searched keywords for a simple recommendation
def get_hotspots(history_str):
    history_list = []
    hotspots = []
    history_str_new = ""
    if history_str:
        history_str_list = history_str.split("&")
        for record in history_str_list:
            key, val = record.split("+")
            history_list.append((key, int(val)))
        hotspots = find_hotest_records(history_list, 3)
        history_str_list_new = []
        for record in history_list:
            history_str_list_new.append("+".join([record[0], str(record[1])]))
        history_str_new = "&".join(history_str_list_new)
    return history_str_new, hotspots


# this function changes the order of items in "records"
def find_hotest_records(records, num):
    length = len(records)
    if num < 1 or num > length:
        num = length
        
    # for a common case in which the top "num" elements stays the same
    records[0], records[num - 1] = records[num - 1], records[0]
    find_hotest_records_helper(records, 0, length-1, num)
    
    hotspots = [""] * num
    for i in range(num):
        hotspots[i] = records[i][0]
    return hotspots


def find_hotest_records_helper(records, start, end, num):
    while start < end:
        pivot = records[start][1]
        bound = start
        for i in range(start + 1, end + 1):
            if records[i][1] > pivot:
                bound += 1
                records[i], records[bound] = records[bound], records[i]
        records[start], records[bound] = records[bound], records[start]
        if bound + 1 == num:
            break
        elif bound + 1 < num:
            start = bound + 1
        else:
            end = bound - 1


# generate recommendation posts from a list of keywords
def gen_recommendations(es, word_lst, filter_lst):
    recommend_query = Config.QUERY.copy()
    recommend_query["size"] = 100
    recommend_query["query"]["multi_match"]["query"] = " ".join(word_lst)
    response = es.search(index="index_jobposts", body=recommend_query)["hits"]["hits"]
    filtered_response = []
    cnt = 0    # show 10 recommended posts a time at most
    for record in response:
        if record["_source"]["post_id"] not in filter_lst:
            cnt += 1
            filtered_response.append(record)
        if cnt == 10:
            break
    return get_post_MySQL(filtered_response)


# used for general search history: move the latest searched keywords to the end of the list
def update_all_history(all_history, words):
    if all_history:
        history_lst = all_history.decode("utf-8").split("&")
        length_lst = len(history_lst)
        for word in words:
            for i in range(length_lst):
                if word == history_lst[i]:
                    history_lst.pop(i)    # ensure all the words in "words" and "history_lst" are unique
                    break
            history_lst.append(word)
            length_lst = len(history_lst)
        # keep the number of keywords <= 15
        while length_lst > 15:
            history_lst.pop(0)
            length_lst -= 1
        return "&".join(history_lst)
    else:
        return "&".join(words) if words != [] else None