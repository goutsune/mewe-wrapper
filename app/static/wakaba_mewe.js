function form_subject(form, num, subject, admin, taskwas, pagewas) {
	var span = document.getElementById('subj' + num);
	var text = '<form method="post" action="' + form + '">' +
	'<input type="hidden" name="admin" value="' + admin + '" />' +
	'<input type="hidden" name="taskwas" value="' + taskwas + '" /><input type="hidden" name="pagewas" value="' + pagewas + '" />' +
	'<input type="hidden" name="mtask" value="subject" /><input type="hidden" name="num" value="' + num + '" />' +
	'<input type="text" name="subject" value="' + subject + '" /><input type="submit" value="S" /></form>';
	span.innerHTML = text;
}

function get_cookie(name)
{
	with(document.cookie)
	{
		var regexp=new RegExp("(^|;\\s+)"+name+"=(.*?)(;|$)");
		var hit=regexp.exec(document.cookie);
		if(hit&&hit.length>2) return unescape(hit[2]);
		else return '';
	}
};

function set_cookie(name,value,days)
{
	if(days)
	{
		var date=new Date();
		date.setTime(date.getTime()+(days*24*60*60*1000));
		var expires="; expires="+date.toGMTString();
	}
	else expires="";
	document.cookie=name+"="+value+expires+"; path=/";
}

function insert_c(comment_id, author_id)
{
	var reply_to=document.forms.postform.reply_to;
	reply_to.value = comment_id;
	var textarea=document.forms.postform.text;

	if(textarea)
	{
		var mention = "@{{u_" + author_id + "}" + comment_id + "}";
		if(textarea.createTextRange && textarea.caretPos) // IE
		{
			var caretPos=textarea.caretPos;
			caretPos.text = caretPos.text.charAt(caretPos.text.length-1) == " "? mention + "\n\n":mention;
		}
		else if(textarea.setSelectionRange) // Firefox
		{
			var start=textarea.selectionStart;
			var end=textarea.selectionEnd;
			textarea.value = textarea.value.substr(0,start) +	mention + "\n\n" + textarea.value.substr(end);
			textarea.setSelectionRange(start+mention.length,start+mention.length);
		}
		else
		{
			textarea.value += mention + "\n\n";
		}
		textarea.focus();
	}
}

function insert_r(parent_id, author_id, comment_id)
{
	insert_c(comment_id, author_id);
	var reply_to=document.forms.postform.reply_to;
	reply_to.value = parent_id;
}

function insert(post_id, author_id)
{
	var textarea=document.forms.postform.text;
	var reply_to=document.forms.postform.reply_to;
	reply_to.value = '';
	if(textarea)
	{
		var mention = "@{{u_" + author_id + "}" + post_id + "}";
		if(textarea.createTextRange && textarea.caretPos) // IE
		{
			var caretPos=textarea.caretPos;
			caretPos.text = caretPos.text.charAt(caretPos.text.length-1) == " "? mention + "\n\n":mention;
		}
		else if(textarea.setSelectionRange) // Firefox
		{
			var start=textarea.selectionStart;
			var end=textarea.selectionEnd;
			textarea.value = textarea.value.substr(0,start) +	mention + "\n\n" + textarea.value.substr(end);
			textarea.setSelectionRange(start+mention.length,start+mention.length);
		}
		else
		{
			textarea.value += mention + "\n\n";
		}
		textarea.focus();
	}
}

function highlight(post)
{
	var cells=document.getElementsByTagName("td");
	for(var i=0;i<cells.length;i++) if(cells[i].className=="highlight") cells[i].className="reply";

	var reply=document.getElementById("reply"+post);
	if(reply)
	{
		reply.className="highlight";
/*		var match=/^([^#]*)/.exec(document.location.toString());
		document.location=match[1]+"#"+post;*/
		return false;
	}

	return true;
}


function set_stylesheet_frame(styletitle,framename)
{
	set_stylesheet(styletitle);
	var list = get_frame_by_name(framename);
	if(list) set_stylesheet(styletitle,list);
}

function set_stylesheet(styletitle,target)
{
	set_cookie("wakabastyle",styletitle,365);

	var links = target ? target.document.getElementsByTagName("link") : document.getElementsByTagName("link");
	var found=false;
	for(var i=0;i<links.length;i++)
	{
		var rel=links[i].getAttribute("rel");
		var title=links[i].getAttribute("title");
		if(rel.indexOf("style")!=-1&&title)
		{
			links[i].disabled=true; // IE needs this to work. IE needs to die.
			if(styletitle==title) { links[i].disabled=false; found=true; }
		}
	}
	if(!found)
	{
		if(target) set_preferred_stylesheet(target);
		else set_preferred_stylesheet();
	}
}

function set_preferred_stylesheet(target)
{
	var links = target ? target.document.getElementsByTagName("link") : document.getElementsByTagName("link");
	for(var i=0;i<links.length;i++)
	{
		var rel=links[i].getAttribute("rel");
		var title=links[i].getAttribute("title");
		if(rel.indexOf("style")!=-1&&title) links[i].disabled=(rel.indexOf("alt")!=-1);
	}
}

function get_active_stylesheet()
{
	var links=document.getElementsByTagName("link");
	for(var i=0;i<links.length;i++)
	{
		var rel=links[i].getAttribute("rel");
		var title=links[i].getAttribute("title");
		if(rel.indexOf("style")!=-1&&title&&!links[i].disabled) return title;
	}
	return null;
}

function get_preferred_stylesheet()
{
	var links=document.getElementsByTagName("link");
	for(var i=0;i<links.length;i++)
	{
		var rel=links[i].getAttribute("rel");
		var title=links[i].getAttribute("title");
		if(rel.indexOf("style")!=-1&&rel.indexOf("alt")==-1&&title) return title;
	}
	return null;
}

function get_frame_by_name(name)
{
	var frames = window.parent.frames;
	for(i = 0; i < frames.length; i++)
	{
		if(name == frames[i].name) { return(frames[i]); }
	}
}

function set_delpass(id)
{
	with(document.getElementById(id)) password.value=get_cookie("password");
}

function do_ban(el)
{
	var reason=prompt("Give a reason for this ban:");
	if(reason) document.location=el.href+"&comment="+encodeURIComponent(reason);
	return false;
}

window.onunload=function(e)
{
	if(style_cookie)
	{
		var title=get_active_stylesheet();
		set_cookie(style_cookie,title,365);
	}
}

if(style_cookie)
{
	var cookie=get_cookie(style_cookie);
	var title=cookie?cookie:get_preferred_stylesheet();
	set_stylesheet(title);
}
